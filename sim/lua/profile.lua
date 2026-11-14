-- Time the parts of G:update while play_dumb runs.
--   python sim/host.py sim/lua/profile.lua [games] [dt]
local function now() return SIM_WALL_TIME() end

PROF = {}
local function wrap(tbl, name, label)
  local f = tbl[name]
  tbl[name] = function(...)
    local t = now()
    local a, b, c = f(...)
    PROF[label] = (PROF[label] or 0) + now() - t
    return a, b, c
  end
end

local after = nil
local orig_after = function()
  wrap(EventManager, 'update', 'E_MANAGER:update')
  wrap(Moveable, 'move', 'Moveable:move')
  wrap(G, 'update', 'G:update total')
  wrap(G.CONTROLLER, 'update', 'CONTROLLER:update')
  wrap(_G, 'set_alerts', 'set_alerts')
  wrap(_G, 'nuGC', 'nuGC')
  for _, cls in ipairs({'Card', 'UIBox', 'UIElement', 'DynaText', 'CardArea', 'Sprite', 'Blind'}) do
    if _G[cls] and rawget(_G[cls], 'update') then wrap(_G[cls], 'update', cls .. ':update') end
    if _G[cls] and rawget(_G[cls], 'move') then wrap(_G[cls], 'move', cls .. ':move') end
  end
  local n = 0; for _ in pairs(G.MOVEABLES) do n = n + 1 end
  print('moveables at menu', n)
end

PROFILE_HOOK = orig_after
dofile('sim/lua/play_dumb.lua')
