"""WSGI entry point for a production WSGI server (Waitress).

``create_app`` builds the Flask app; this module exposes it as a module-level
``app`` so a server can load it by ``wsgi:app``, e.g.::

    waitress-serve --listen=127.0.0.1:8080 --threads=50 wsgi:app

Unlike ``main.py`` (the Werkzeug development server, for local work only), this
entry point never starts the auto-reloader or the interactive debugger. See the
deployment notes in ``documents/project_requirements.md`` (§18).
"""

from app import create_app

app = create_app()
