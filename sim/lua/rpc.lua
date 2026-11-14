-- Call balatrobot endpoints in-process: dispatch the request, then step frames until the
-- endpoint's send_response fires. Returns the response table and the frames it took.

local pending
BB_DISPATCHER.Server = {send_response = function(r) pending = r; return true end}

RPC_STATS = {}

function rpc(method, params, max_frames, dt)
  pending = nil
  local t0 = os.clock()
  BB_DISPATCHER.dispatch({jsonrpc = '2.0', method = method, params = params or {}, id = 1})
  local frames = 0
  while pending == nil do
    if frames >= (max_frames or 20000) then error(('%s: no response after %d frames (STATE=%s)'):format(method, frames, tostring(G.STATE))) end
    step(dt)
    frames = frames + 1
  end
  -- The live game keeps running frames while the bot decides; gates like
  -- Card:can_use_consumeable refuse until STOP_USE has counted down and the controller unlocks.
  local settle = 0
  while settle < 300 and ((G.GAME and G.GAME.STOP_USE or 0) > 0 or G.CONTROLLER.locked) do
    step(dt)
    settle = settle + 1
  end
  frames = frames + settle
  local s = RPC_STATS[method] or {n = 0, frames = 0, secs = 0}
  s.n, s.frames, s.secs = s.n + 1, s.frames + frames, s.secs + (os.clock() - t0)
  RPC_STATS[method] = s
  return pending, frames
end

function print_rpc_stats()
  local names = {}
  for k in pairs(RPC_STATS) do names[#names + 1] = k end
  table.sort(names)
  print(('%-12s %6s %10s %10s %10s'):format('method', 'calls', 'frames/call', 'ms/call', 'ms/frame'))
  for _, k in ipairs(names) do
    local s = RPC_STATS[k]
    print(('%-12s %6d %10.1f %10.2f %10.3f'):format(k, s.n, s.frames / s.n, 1000 * s.secs / s.n, s.frames > 0 and 1000 * s.secs / s.frames or 0))
  end
end
