from fastapi.middleware.cors import CORSMiddleware

def setup_middleware(app):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173","https://godwin-xavier864.github.io"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )