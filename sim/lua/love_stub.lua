-- Headless stand-in for the LÖVE 11.5 API: every graphics/audio/window call is a no-op,
-- filesystem reads go to the game source tree, and threads/channels run inline.
-- Unknown love.<module>.<fn> calls resolve to a null object and are tallied in STUB_MISSES.

local SRC_ROOT = assert(SIM_SRC_ROOT, 'set SIM_SRC_ROOT before loading love_stub')
local SAVE_ROOT = SIM_SAVE_ROOT  -- nil: saves are discarded

STUB_MISSES = {}

-- A value that absorbs any use: index, call, arithmetic, comparison-free.
local Null = {}
local null_mt = {}
null_mt.__index = function() return Null end
null_mt.__call = function() return Null end
null_mt.__newindex = function() end
null_mt.__tostring = function() return 'Null' end
setmetatable(Null, null_mt)
SIM_NULL = Null

local function noop() end

local function module(name, fns)
  return setmetatable(fns, {__index = function(t, k)
    local key = name .. '.' .. k
    STUB_MISSES[key] = (STUB_MISSES[key] or 0) + 1
    return function() return Null end
  end})
end

local function file_exists(p)
  local f = io.open(p, 'rb'); if f then f:close() return true end
end

local function read_file(p)
  local f = io.open(p, 'rb'); if not f then return nil end
  local s = f:read('*a'); f:close(); return s
end

-- Image/canvas-like object: answers size queries, swallows everything else.
local function drawable(w, h)
  local o = {}
  return setmetatable(o, {__index = function(_, k)
    if k == 'getDimensions' then return function() return w, h end end
    if k == 'getWidth' then return function() return w end end
    if k == 'getHeight' then return function() return h end end
    return noop
  end})
end

local SCREEN_W, SCREEN_H = 1920, 1080

love = {}
love.arg = {parseGameArguments = function(a) return a end}

love.system = module('system', {
  getOS = function() return 'Windows' end,
  openURL = noop, setClipboardText = noop,
  getClipboardText = function() return '' end,
  getProcessorCount = function() return 1 end,
})

local ffi = require('ffi')
ffi.cdef[[int QueryPerformanceCounter(int64_t *c); int QueryPerformanceFrequency(int64_t *f);]]
local qpc, qpf = ffi.new('int64_t[1]'), ffi.new('int64_t[1]')
ffi.C.QueryPerformanceFrequency(qpf)
local QPF = tonumber(qpf[0])
-- Microsecond wall clock: nuGC spends a 0.3 ms budget per frame measured with this.
local function wall_time() ffi.C.QueryPerformanceCounter(qpc); return tonumber(qpc[0]) / QPF end
SIM_WALL_TIME = wall_time

love.timer = module('timer', {
  getTime = wall_time, step = function() return 1/60 end, sleep = noop,
  getFPS = function() return 60 end, getDelta = function() return 1/60 end,
})

love.graphics = module('graphics', {
  getWidth = function() return SCREEN_W end,
  getHeight = function() return SCREEN_H end,
  getDimensions = function() return SCREEN_W, SCREEN_H end,
  isActive = function() return false end,
  isCreated = function() return true end,
  newImage = function() return drawable(1024, 1024) end,
  newCanvas = function(w, h) return drawable(w or SCREEN_W, h or SCREEN_H) end,
  newShader = function() return drawable(0, 0) end,
  newFont = function() return drawable(0, 0) end,
  setNewFont = function() return drawable(0, 0) end,
  newText = function() return drawable(10, 10) end,
  newQuad = function() return drawable(0, 0) end,
  newVideo = function() return drawable(0, 0) end,
  getFont = function() return drawable(0, 0) end,
  push = noop, pop = noop, setColor = noop, translate = noop, scale = noop, rotate = noop,
  draw = noop, rectangle = noop, setShader = noop, setLineWidth = noop, clear = noop,
  setCanvas = noop, print = noop, printf = noop, present = noop, polygon = noop,
  setLineStyle = noop, setDefaultFilter = noop, reset = noop, origin = noop,
  setBlendMode = noop, circle = noop, line = noop, setScissor = noop,
})

love.window = module('window', {
  getMode = function() return SCREEN_W, SCREEN_H, {fullscreen = false, display = 1, vsync = 0} end,
  setMode = function() return true end, updateMode = function() return true end,
  getDesktopDimensions = function() return SCREEN_W, SCREEN_H end,
  getFullscreenModes = function() return {{width = SCREEN_W, height = SCREEN_H}} end,
  getDisplayCount = function() return 1 end,
  toPixels = function(v) return v end, fromPixels = function(v) return v end, isOpen = function() return true end,
  getTitle = function() return 'Balatro' end, setTitle = noop, showMessageBox = noop,
  getDisplayName = function() return 'sim' end, setIcon = noop,
})

love.mouse = module('mouse', {
  setVisible = noop, setGrabbed = noop, setRelativeMode = noop,
  isVisible = function() return false end, getPosition = function() return 0, 0 end,
  isDown = function() return false end,
})
love.touch = module('touch', {getTouches = function() return {} end})
love.joystick = module('joystick', {getJoysticks = function() return {} end, loadGamepadMappings = noop})
love.keyboard = module('keyboard', {isDown = function() return false end})
love.event = module('event', {pump = noop, poll = function() return function() end end, quit = noop})

local audio_source = setmetatable({}, {__index = function() return function() return 0 end end})
love.audio = module('audio', {
  play = noop, stop = noop, newSource = function() return audio_source end,
  setVolume = noop, setPosition = noop,
})
love.sound = module('sound', {newSoundData = function() return Null end})

-- Filesystem: reads resolve against the game source, writes go to SAVE_ROOT or nowhere.
local VANILLA_ROOT = SIM_VANILLA_ROOT or SRC_ROOT
local function src_path(p)
  local d = SRC_ROOT .. '/' .. p
  if file_exists(d) then return d end
  return VANILLA_ROOT .. '/' .. p
end
local function save_path(p) return SAVE_ROOT and (SAVE_ROOT .. '/' .. p) end

local function list_dir(dir)
  local items = {}
  local p = io.popen('dir /b "' .. dir:gsub('/', '\\') .. '" 2>nul')
  if p then for line in p:lines() do items[#items + 1] = line end p:close() end
  return items
end

love.filesystem = module('filesystem', {
  getInfo = function(p)
    local sp = save_path(p)
    if (sp and file_exists(sp)) or file_exists(src_path(p)) then return {type = 'file'} end
    return nil
  end,
  read = function(p)
    local sp = save_path(p)
    local s = (sp and read_file(sp)) or read_file(src_path(p))
    if s then return s, #s end
    return nil, 'not found'
  end,
  write = function(p, data)
    local sp = save_path(p); if not sp then return true end
    local f = io.open(sp, 'wb'); f:write(data); f:close(); return true
  end,
  append = function() return true end,
  remove = function() return true end,
  createDirectory = function() return true end,
  getDirectoryItems = function(p)
    -- Sound and texture assets are not snapshotted; the game only needs their names.
    if p == 'resources/sounds' then return SIM_SOUND_NAMES or {} end
    return list_dir(VANILLA_ROOT .. '/' .. p)
  end,
  getSaveDirectory = function() return SAVE_ROOT or '' end,
  getSourceBaseDirectory = function() return SRC_ROOT end,
  getIdentity = function() return 'Balatro' end, setIdentity = noop,
  isFused = function() return true end,
  getRealDirectory = function() return SRC_ROOT end,
  mount = function() return true end,
  load = function(p)
    local s = love.filesystem.read(p)
    if not s then return nil, p .. ' not found' end
    return loadstring(s, '@' .. p)
  end,
})

-- love.data: compress/decompress are identity; save blobs stay plain Lua text.
love.data = module('data', {
  compress = function(_, _, s) return s end,
  decompress = function(_, _, s) return s end,
})

-- Threads: the save/sound/http workers are dropped; channels are plain queues.
local channels = {}
local function new_channel()
  local q = {}
  return {
    push = function(_, v) q[#q + 1] = v end,
    pop = function() return table.remove(q, 1) end,
    demand = function() return table.remove(q, 1) end,
    peek = function() return q[1] end,
    getCount = function() return #q end,
    clear = function() for i = #q, 1, -1 do q[i] = nil end end,
  }
end
-- Worker channels nobody reads in the sim; pushing to them would only grow memory.
SINK_CHANNELS = {sound_request = true, save_request = true, http_request = true}
local sink = {push = noop, pop = noop, demand = noop, peek = noop, getCount = function() return 0 end, clear = noop}
love.thread = module('thread', {
  newThread = function() return {start = noop, wait = noop, isRunning = function() return false end} end,
  getChannel = function(name)
    if SINK_CHANNELS[name] then return sink end
    channels[name] = channels[name] or new_channel(); return channels[name]
  end,
  newChannel = new_channel,
})

love.handlers = setmetatable({}, {__index = function() return noop end})


love.image = module('image', {newImageData = function() return drawable(1024, 1024) end})
-- Any other love.<module> resolves to a tallied null module.
setmetatable(love, {__index = function(t, k)
  if type(k) ~= 'string' or k:match('^[a-z]+$') == nil then return nil end
  local m = module(k, {}); rawset(t, k, m); return m
end})
return love
