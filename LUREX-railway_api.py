"""Railway web-service entrypoint for the LUREX API and loader."""

import importlib.util
import os
import sys
from http.server import ThreadingHTTPServer


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

try:
    from api.index import handler
except Exception:
    # Compatibility for the current GitHub upload where package files are at root.
    package_init = os.path.join(BASE_DIR, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "darcobfuscator",
        package_init,
        submodule_search_locations=[BASE_DIR],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["darcobfuscator"] = package
    spec.loader.exec_module(package)
    from index import handler


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"LUREX API listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
