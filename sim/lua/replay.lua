-- Replay a recorded request log through the endpoints and write a state fingerprint per action.
--   python sim/host.py sim/lua/replay.lua RECORD.jsonl OUT.txt [dt] [nogc]
local RECORD_PATH, OUT_PATH = arg[1], arg[2]
local DT = tonumber(arg[3]) or (4.99 / 60)
local OPTS = arg[4] or ''
arg = {'dump'}

local function canon(v, out)
  local t = type(v)
  if t == 'table' then
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = k end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    out[#out + 1] = '{'
    for _, k in ipairs(keys) do out[#out + 1] = tostring(k) .. '='; canon(v[k], out); out[#out + 1] = ',' end
    out[#out + 1] = '}'
  elseif t == 'number' then
    out[#out + 1] = ('%.10g'):format(v)
  else
    out[#out + 1] = tostring(v)
  end
  return out
end

SIM_AFTER_BOOT = function()
  if OPTS:find('nogc') then nuGC = function() end; collectgarbage('restart') end
  dofile(SIM_LUA_DIR .. '/rpc.lua')
  local json = require('json')
  local out = assert(io.open(OUT_PATH, 'w'))
  local t0, n = os.clock(), 0
  for line in io.lines(RECORD_PATH) do
    local req = json.decode(line)
    local r = rpc(req.method, req.params, nil, DT)
    n = n + 1
    -- The whole gamestate the bot sees, keys sorted, so any rules drift shows as a diff.
    out:write(n, ' ', req.method, ' ', table.concat(canon(r, {})), '\n')
  end
  out:close()
  print(('replayed %d requests in %.2fs, %d frames'):format(n, os.clock() - t0, FRAMES))
  print_rpc_stats()
end
dofile(SIM_LUA_DIR and (SIM_LUA_DIR .. '/boot.lua') or 'sim/lua/boot.lua')
