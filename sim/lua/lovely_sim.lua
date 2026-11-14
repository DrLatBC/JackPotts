-- Stand-in for the Lovely injector: registers each mod's [patches.module] entries as
-- package.preload loaders and serves `require "lovely"`. Pattern/copy patches are not
-- re-applied here; the game files come from Lovely's own game-dump, which has them.

local MODS_DIR = assert(SIM_MODS_DIR)
local DUMP_DIR = assert(SIM_SRC_ROOT)

local function read_file(p)
  local f = io.open(p, 'rb'); if not f then return nil end
  local s = f:read('*a'); f:close(); return s
end

local function list_dir(dir, want_dirs)
  local items = {}
  local flag = want_dirs == nil and '' or (want_dirs and ' /ad' or ' /a-d')
  local p = io.popen('dir /b' .. flag .. ' "' .. dir:gsub('/', '\\') .. '" 2>nul')
  if p then for line in p:lines() do items[#items + 1] = line end p:close() end
  return items
end
SIM_LIST_DIR = list_dir

-- Minimal TOML walk: only [[patches]] blocks whose table is [patches.module].
local function module_patches(toml)
  local out, cur = {}, nil
  for line in (toml .. '\n'):gmatch('([^\n]*)\n') do
    line = line:gsub('\r$', '')
    if line:match('^%s*%[%[patches%]%]') then cur = nil
    elseif line:match('^%s*%[patches%.module%]') then cur = {}; out[#out + 1] = cur
    elseif line:match('^%s*%[') then cur = nil
    elseif cur then
      local k, v = line:match('^%s*([%w_]+)%s*=%s*["\'](.-)["\']')
      if k then cur[k] = v end
      local kb, vb = line:match('^%s*([%w_]+)%s*=%s*(%a+)%s*$')
      if kb then cur[kb] = (vb == 'true') end
    end
  end
  return out
end

-- Lovely dumps module sources it patched under game-dump/lovely/<name with dots as dirs>/<source>.
local function dumped_module(name, source)
  return read_file(DUMP_DIR .. '/lovely/' .. name:gsub('%.', '/') .. '/' .. source)
end

LOVELY_MODULES = {}
LOVELY_LOAD_NOW = {}
for _, mod in ipairs(list_dir(MODS_DIR, true)) do
  local lovely_dir = MODS_DIR .. '/' .. mod .. '/lovely'
  local tomls = list_dir(lovely_dir, false)
  local root_toml = read_file(MODS_DIR .. '/' .. mod .. '/lovely.toml')
  local sources = {}
  for _, t in ipairs(tomls) do if t:match('%.toml$') then sources[#sources + 1] = read_file(lovely_dir .. '/' .. t) end end
  if root_toml then sources[#sources + 1] = root_toml end
  for _, toml in ipairs(sources) do
    for _, m in ipairs(module_patches(toml)) do
      local path = MODS_DIR .. '/' .. mod .. '/' .. m.source
      local src = dumped_module(m.name, m.source) or assert(read_file(path), path)
      LOVELY_MODULES[m.name] = path
      package.preload[m.name] = function(...)
        local chunk = assert(loadstring(SIM_MODULE_FIXUP(m.name, src), '=[lovely ' .. m.name .. ' "' .. m.source .. '"]'))
        return chunk(...)
      end
      if m.load_now then LOVELY_LOAD_NOW[#LOVELY_LOAD_NOW + 1] = m.name end
    end
  end
end

local lovely_dir_abs = MODS_DIR:gsub('\\', '/')
package.preload.lovely = function()
  return {
    version = '0.8.0-sim',
    mod_dir = lovely_dir_abs .. '/',
    reload_patches = function() return true end,
    apply_patches = function(_, src) return src end,
    set_var = function() end, remove_var = function() end,
  }
end
