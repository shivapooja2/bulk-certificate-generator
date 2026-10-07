from app import create_app

app = create_app()

if __name__ == "__main__":
    # use_reloader=False: the reloader would start a second process (and a second worker pool).
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
