from app.main import app


if __name__ == "__main__":
    import threading
    import uvicorn
    import webbrowser

    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8000")).start()

    uvicorn.run(app, host="127.0.0.1", port=8000)