-- Play whole games through balatrobot's endpoints with a trivial policy, to time the sim.
--   python sim/host.py sim/lua/play_dumb.lua [games] [dt]
-- Policy: always select the blind, play the first five cards, cash out, leave the shop.

local GAMES = tonumber(arg[1]) or 1
local DT = tonumber(arg[2]) or 1/60
FINGERPRINT = arg[3] and io.open(arg[3], 'w')

-- Everything rules-relevant the bot sees after an action, in a stable order.
function fingerprint(method, r)
  local parts = {method, tostring(r.state), 'ante=' .. tostring(r.ante_num), 'round=' .. tostring(r.round_num),
                 '$' .. tostring(r.money)}
  if r.round then
    parts[#parts + 1] = 'chips=' .. tostring(r.round.chips)
    parts[#parts + 1] = 'hands=' .. tostring(r.round.hands_left) .. ' disc=' .. tostring(r.round.discards_left)
  end
  for _, area in ipairs({'hand', 'jokers', 'consumables', 'shop', 'vouchers', 'packs'}) do
    local a = r[area]
    if a and a.cards then
      local ids = {}
      for _, c in ipairs(a.cards) do ids[#ids + 1] = tostring(c.key) end
      parts[#parts + 1] = area .. '=[' .. table.concat(ids, ',') .. ']'
    end
  end
  return table.concat(parts, ' ')
end
arg = {'dump'}
SIM_AFTER_BOOT = function()
  dofile(SIM_LUA_DIR .. '/rpc.lua')
  if PROFILE_HOOK then PROFILE_HOOK() end
  local total_t0 = os.clock()
  for g = 1, GAMES do
    local t0, f0 = os.clock(), FRAMES
    local r = rpc('start', {deck = 'BLUE', stake = 'WHITE', seed = 'B2WTM8V4'}, nil, DT)
    if r.message then error('start: ' .. r.message) end
    local actions, result = 0, nil
    while true do
      local s = r.state
      if s == 'GAME_OVER' then result = 'lost'; break end
      if r.won then result = 'won'; break end
      local method, params
      if s == 'BLIND_SELECT' then method = 'select'
      elseif s == 'SELECTING_HAND' then
        local n = math.min(5, #r.hand.cards)
        local idx = {}
        for i = 0, n - 1 do idx[#idx + 1] = i end
        method, params = 'play', {cards = idx}
      elseif s == 'ROUND_EVAL' then method = 'cash_out'
      elseif s == 'SHOP' then method = 'next_round'
      else error('unhandled state ' .. tostring(s)) end
      r = rpc(method, params, nil, DT)
      actions = actions + 1
      if FINGERPRINT then FINGERPRINT:write(fingerprint(method, r), '\n') end
      if r.message then error(method .. ': ' .. r.message) end
    end
    print(('game %d: %s at ante %s round %s, %d actions, %d frames, %.2fs'):format(
      g, result, tostring(r.ante_num), tostring(r.round_num), actions, FRAMES - f0, os.clock() - t0))
    rpc('menu', nil, nil, DT)
  end
  print(('%d games in %.2fs'):format(GAMES, os.clock() - total_t0))
  print_rpc_stats()
  if PROF then
    local ks = {}; for k in pairs(PROF) do ks[#ks + 1] = k end
    table.sort(ks, function(a, b) return PROF[a] > PROF[b] end)
    for _, k in ipairs(ks) do print(('%-22s %8.1f ms  %6.3f ms/frame'):format(k, PROF[k] * 1000, PROF[k] * 1000 / FRAMES)) end
    local n = 0; for _ in pairs(G.MOVEABLES) do n = n + 1 end
    print('moveables at end', n, 'frames', FRAMES)
  end
end
dofile('sim/lua/boot.lua')
