"""Run a Lua file in the LuaJIT that ships with Balatro (lua51.dll), no LÖVE.

Usage: python sim/host.py <script.lua> [args...] [--serve PORT]
The script sees its args in the global `arg` table. With --serve, once the script
returns the host answers JSON-RPC POSTs on 127.0.0.1:PORT by calling the Lua
global sim_handle(body) -> response body, one request at a time.
"""
import ctypes
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(HERE, "runtime")

os.add_dll_directory(RUNTIME)
lua = ctypes.CDLL(os.path.join(RUNTIME, "lua51.dll"))
lua.luaL_newstate.restype = ctypes.c_void_p
lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
lua.lua_tolstring.restype = ctypes.c_void_p
lua.lua_createtable.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
lua.lua_pushstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lua.lua_pushlstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
lua.lua_rawseti.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
lua.lua_setfield.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
lua.lua_getfield.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
lua.lua_settop.argtypes = [ctypes.c_void_p, ctypes.c_int]

LUA_GLOBALSINDEX = -10002
BOOTSTRAP = (b"local f = assert(loadfile(SIM_SCRIPT)); "
             b"local ok, e = xpcall(f, debug.traceback); if not ok then error(e, 0) end")
HANDLE = b"local ok, r = xpcall(sim_handle, debug.traceback, ...); if not ok then error(r, 0) end; return r"


def to_bytes(L, idx):
    n = ctypes.c_size_t()
    p = lua.lua_tolstring(L, idx, ctypes.byref(n))
    return ctypes.string_at(p, n.value)


def serve(L, port):
    lua.luaL_loadstring(L, HANDLE)
    lua.lua_setfield(L, LUA_GLOBALSINDEX, b"SIM_HANDLE_CHUNK")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            lua.lua_getfield(L, LUA_GLOBALSINDEX, b"SIM_HANDLE_CHUNK")
            lua.lua_pushlstring(L, body, len(body))
            if lua.lua_pcall(L, 1, 1, 0):
                print("LUA ERROR:", to_bytes(L, -1).decode(errors="replace"), file=sys.stderr, flush=True)
                lua.lua_settop(L, 0)
                self.send_response(500)
                self.end_headers()
                return
            out = to_bytes(L, -1)
            lua.lua_settop(L, 0)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    print(f"serving on 127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    argv = sys.argv[1:]
    port = None
    if "--serve" in argv:
        i = argv.index("--serve")
        port = int(argv[i + 1])
        del argv[i:i + 2]
    script, *args = argv
    L = lua.luaL_newstate()
    lua.luaL_openlibs(L)
    lua.lua_createtable(L, len(args), 0)
    for i, a in enumerate(args, 1):
        lua.lua_pushstring(L, a.encode())
        lua.lua_rawseti(L, -2, i)
    lua.lua_setfield(L, LUA_GLOBALSINDEX, b"arg")
    lua.lua_pushstring(L, script.encode())
    lua.lua_setfield(L, LUA_GLOBALSINDEX, b"SIM_SCRIPT")
    if lua.luaL_loadstring(L, BOOTSTRAP) or lua.lua_pcall(L, 0, 0, 0):
        sys.stdout.flush()
        print("LUA ERROR:", to_bytes(L, -1).decode(errors="replace"), file=sys.stderr)
        sys.exit(1)
    if port is not None:
        serve(L, port)


if __name__ == "__main__":
    main()
