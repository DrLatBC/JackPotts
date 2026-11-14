-- Boot Balatro's Lua headless.
--   python sim/host.py sim/lua/boot.lua vanilla|dump
-- vanilla: the unmodded game.  dump: Lovely's patched game files + Steamodded + balatrobot.
io.stdout:setvbuf('no')
local MODE = arg[1] or 'vanilla'
local CWD = io.popen('cd'):read('*l'):gsub('\\', '/')
SIM_LUA_DIR = CWD .. '/sim/lua'
SIM_SRC_ROOT = CWD .. '/sim/lua_src/' .. MODE
SIM_MODS_DIR = CWD .. '/sim/lua_src/mods'
SIM_SAVE_ROOT = nil

SIM_SOUND_NAMES = {}
for line in io.lines(SIM_LUA_DIR .. '/sound_names.txt') do SIM_SOUND_NAMES[#SIM_SOUND_NAMES + 1] = line end

-- Lovely dumps only the files it patched; everything else loads from the vanilla archive.
SIM_VANILLA_ROOT = CWD .. '/sim/lua_src/vanilla'
dofile(SIM_LUA_DIR .. '/love_stub.lua')
package.path = SIM_SRC_ROOT .. '/?.lua;' .. SIM_VANILLA_ROOT .. '/?.lua;' .. SIM_LUA_DIR .. '/?.lua;' .. package.path
package.preload.luasteam = function() return nil end

local t0 = os.clock()
if MODE == 'dump' then
  dofile(SIM_LUA_DIR .. '/modded_runtime.lua')
end
dofile(SIM_SRC_ROOT .. '/main.lua')
print(('main.lua loaded in %.3fs'):format(os.clock() - t0))

-- A fresh profile would start the tutorial (seed "TUTORIAL"); the bot's profiles have finished it.
G.SETTINGS.tutorial_complete = true
t0 = os.clock()
G:start_up()
print(('G:start_up done in %.3fs, STATE=%s STAGE=%s'):format(os.clock() - t0, tostring(G.STATE), tostring(G.STAGE)))

if MODE == 'vanilla' then
  -- balatrobot's configure_settings, the parts that change game behaviour or speed
  G.SETTINGS.SOUND.volume = 0; G.F_SOUND_THREAD = false; G.F_MUTE = true
  G.SETTINGS.GAMESPEED = 4
  G.F_SKIP_TUTORIAL = true
  G.SETTINGS.tutorial_complete = true
  G.SETTINGS.skip_splash = 'Yes'
  G.SETTINGS.reduced_motion = true
end

FRAMES = 0
function step(dt) FRAMES = FRAMES + 1; love.update(dt or 1/60) end
function step_until(pred, max, dt)
  for i = 1, max or 100000 do
    if pred() then return true, i end
    step(dt)
  end
  return false
end

if MODE == 'dump' then
  -- Gradients are UI colours; their update indexes colours[0] whenever TIMERS.REAL lands on
  -- a whole cycle, which large dt steps hit (Steamodded src/game_object.lua:631).
  for _, grad in pairs(SMODS.Gradients) do grad.update = function() end end
  -- balatrobot's love.update pins dt and polls its TCP server; the sim keeps its
  -- per-frame game-over/win checks but chooses dt itself and has no server.
  love.update = function(dt)
    BB_GAMESTATE.check_game_over()
    BB_GAMESTATE.check_win_overlay()
    G:update(dt)
  end
end

local ok, n = step_until(function() return G.STATE == G.STATES.MENU end, 20000)
print('reached MENU', ok, n, 'frames')

-- The profile's unlocks shape shop and pack pools. SIM_UNLOCK_ALL=1 applies the unlock half
-- of the profile screen's "Unlock all" (G.FUNCS.unlock_all, functions/button_callbacks.lua).
if os.getenv('SIM_UNLOCK_ALL') == '1' then
  G.PROFILES[G.SETTINGS.profile].all_unlocked = true
  for _, pool in ipairs({G.P_CENTERS, G.P_BLINDS, G.P_TAGS}) do
    for _, v in pairs(pool) do
      if not v.demo and not v.wip then v.alerted = true; v.discovered = true; v.unlocked = true end
    end
  end
  set_profile_progress()
  set_discover_tallies()
  print('profile: all unlocked')
end

if SIM_AFTER_BOOT then return SIM_AFTER_BOOT() end

t0 = os.clock()
pcall(function() G.FUNCS.setup_run({config = {}}); G.FUNCS.exit_overlay_menu() end)
for _, d in pairs(G.P_CENTER_POOLS.Back) do
  if d.name == 'Blue Deck' then G.GAME.selected_back:change_to(d); G.GAME.viewed_back:change_to(d) end
end
G.FUNCS.start_run(nil, {stake = 1, seed = 'B2WTM8V4'})
ok, n = step_until(function()
  return G.GAME.blind_on_deck ~= nil and G.blind_select_opts ~= nil
     and G.blind_select_opts.small:get_UIE_by_ID('tag_Small') ~= nil
end, 20000)
print('run started', ok, n, 'frames', ('%.3fs'):format(os.clock() - t0))
print('seed', G.GAME.pseudorandom.seed, 'deck', G.GAME.selected_back.name, 'state', G.STATE, 'dollars', G.GAME.dollars)
local ids = {}
for i, c in ipairs(G.deck.cards) do if i <= 8 then ids[#ids+1] = c.base.id .. c.base.suit:sub(1,1) end end
print('top deck', table.concat(ids, ' '), 'n=', #G.deck.cards, 'hands', G.GAME.round_resets.hands)
for k, v in pairs(STUB_MISSES) do print('stub miss', k, v) end
