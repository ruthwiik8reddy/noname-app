# from src import create_app

# app = create_app()

# if __name__ == "__main__":
#     app.run(debug=True, port=5055)



import os
from src import create_app

app = create_app()

if __name__ == "__main__":
    # debug must stay off when the port is reachable from outside this machine —
    # the Werkzeug debugger exposes an interactive Python console.
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host="127.0.0.1", port=5055)