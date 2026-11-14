-- Runtime pieces Steamodded and balatrobot expect beyond the LÖVE API.

local SIM_DIR_ABS = SIM_MODS_DIR:gsub('/sim/lua_src/mods$', '/sim')
SIM_SAVE_ROOT = SIM_DIR_ABS .. '/runtime/save'
os.execute('mkdir "' .. SIM_SAVE_ROOT:gsub('/', '\\') .. '" 2>nul')
love.filesystem.getSaveDirectory = function() return SIM_SAVE_ROOT end
love.filesystem.getSourceBaseDirectory = function() return SIM_DIR_ABS .. '/lua_src' end

love.math = {
  random = math.random,
  newRandomGenerator = function() return {random = function(_, ...) return math.random(...) end, setSeed = function() end} end,
}
local ffi = require('ffi')
love.data.newByteData = function(n)
  local buf = ffi.new('unsigned char[?]', n)
  return {
    getFFIPointer = function() return buf end,
    getString = function() return ffi.string(buf, n) end,
    getSize = function() return n end,
    release = function() end,
  }
end
love.filesystem.newFileData = function(str, name)
  if type(str) == 'table' then str = str:getString() end
  return {getString = function() return str end, getFilename = function() return name end,
          getSize = function() return #str end, release = function() end}
end
love.getVersion =function() return 11, 5, 0, 'Mysterious Mysteries' end

function SIM_MODULE_FIXUP(name, src)
  if name == 'SMODS.preflight.core' then
    -- Lovely writes the real Mods path into this line; point it at the snapshot instead.
    src = src:gsub('local lovely_path = %b[]', 'local lovely_path = [[' .. SIM_MODS_DIR .. '/Steamodded/]]', 1)
  elseif name == 'SMODS.nativefs' or name == 'nativefs' then
    -- nativefs mounts directories through love.dll's PhysFS; the sim lists them with io instead.
    src = src:gsub("ffi%.load%('love'%)", 'SIM_LOVEC', 1)
    src = src:gsub('\nreturn nativefs%s*$', '\nreturn SIM_PATCH_NATIVEFS(nativefs)\n')
  end
  return src
end

SIM_LOVEC = {
  PHYSFS_mount = function() return 0 end,
  PHYSFS_unmount = function() return 0 end,
  PHYSFS_getMountPoint = function() return nil end,
}

function SIM_PATCH_NATIVEFS(nfs)
  local function abs(p)
    if p:match('^%a:[/\\]') or p:match('^/') then return p end
    return nfs.getWorkingDirectory() .. '/' .. p
  end
  local function kind(p)
    local w = abs(p):gsub('/', '\\'):gsub('\\$', '')
    local h = io.popen('if exist "' .. w .. '\\*" (echo directory) else if exist "' .. w .. '" (echo file)')
    local r = h:read('*l'); h:close()
    return r
  end
  nfs.getDirectoryItems = function(dir) return SIM_LIST_DIR(abs(dir)) end
  nfs.getInfo = function(p, filter)
    local t = kind(p)
    if not t or (filter and filter ~= t) then return nil end
    return {type = t, size = 0, modtime = 0}
  end
  nfs.getDirectoryItemsInfo = function(dir, filter)
    local out = {}
    for _, name in ipairs(SIM_LIST_DIR(abs(dir))) do
      local t = kind(dir .. '/' .. name)
      if not filter or filter == t then out[#out + 1] = {name = name, type = t, size = 0, modtime = 0} end
    end
    return out
  end
  return nfs
end

-- balatrobot's TCP server: an inert socket so BB_SERVER.init succeeds; the sim calls
-- the dispatcher directly instead of serving JSON-RPC.
package.preload.socket = function()
  local server = setmetatable({}, {__index = function() return function() return nil, 'timeout' end end})
  server.bind = function() return 1 end
  server.listen = function() return 1 end
  server.setoption = function() return 1 end
  server.settimeout = function() end
  server.getsockname = function() return '127.0.0.1', 0 end
  return {
    tcp = function() return server end,
    connect = function() return nil end,
    bind = function() return server end,
    gettime = os.clock,
  }
end

dofile(SIM_LUA_DIR .. '/lovely_sim.lua')
for _, name in ipairs(LOVELY_LOAD_NOW) do require(name) end

-- LÖVE's utf8 module; only Steamodded's crash screen requires it.
package.preload.utf8 = function() return {charpattern = '[%z\1-\127\194-\244][\128-\191]*'} end
