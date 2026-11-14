-- Boot the modded game and expose sim_handle(body) for host.py's JSON-RPC server.
--   python sim/host.py sim/lua/serve.lua [dt] [nogc] --serve PORT
local DT = tonumber(arg[1]) or 1
local OPTS = arg[2] or ''
arg = {'dump'}
SIM_AFTER_BOOT = function()
  if OPTS:find('nogc') then
    -- nuGC spends a fixed 0.3 ms of wall time per frame on GC steps and stops the
    -- collector otherwise; the sim lets LuaJIT's own incremental GC run instead.
    nuGC = function() end
    collectgarbage('restart')
  end
  dofile(SIM_LUA_DIR .. '/rpc.lua')
  local json = require('json')
  -- SIM_RECORD=path logs every state-changing request as a JSON line, for replay.lua.
  RECORD = os.getenv('SIM_RECORD') and io.open(os.getenv('SIM_RECORD'), 'w')

  -- Mirrors BB_SERVER.send_response's JSON-RPC envelope.
  function sim_handle(body)
    local ok, req = pcall(json.decode, body)
    if not ok or type(req) ~= 'table' then
      return json.encode({jsonrpc = '2.0', error = {code = -32700, message = 'Parse error'}})
    end
    if req.method == 'sim.stats' then
      local out = {frames = FRAMES}
      for k, s in pairs(RPC_STATS) do out[k] = {calls = s.n, frames = s.frames, secs = s.secs} end
      if req.params and req.params.reset then RPC_STATS = {}; FRAMES = 0 end
      return json.encode({jsonrpc = '2.0', result = out, id = req.id})
    end
    if RECORD and req.method ~= 'gamestate' and req.method ~= 'health' then
      RECORD:write(json.encode({method = req.method, params = req.params}), '\n'); RECORD:flush()
    end
    local r = rpc(req.method, req.params, nil, DT)
    if r.message then
      local name = r.name or BB_ERROR_NAMES.INTERNAL_ERROR
      return json.encode({jsonrpc = '2.0', id = req.id,
        error = {code = BB_ERROR_CODES[name] or BB_ERROR_CODES.INTERNAL_ERROR, message = r.message, data = {name = name}}})
    end
    return json.encode({jsonrpc = '2.0', result = r, id = req.id})
  end
  print('sim ready')
end
dofile('sim/lua/boot.lua')
