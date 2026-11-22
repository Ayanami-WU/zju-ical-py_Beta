import os

from waitress import serve

from server.app import create_app

if __name__ == "__main__":
    os.umask(0o077)
    app = create_app()
    serve(
        app,
        host="0.0.0.0",
        port=5273,
        threads=4,
        max_request_body_size=16384,
        clear_untrusted_proxy_headers=not bool(app.extensions["settings"].proxy_hops),
    )
