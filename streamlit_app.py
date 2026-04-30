"""Deprecated Streamlit entrypoint.

This project now deploys as FastAPI serving `frontend/index.html`.
Use: uvicorn backend.main:app --host 0.0.0.0 --port $PORT
"""

if __name__ == "__main__":
    print("Streamlit is no longer used. Start with: uvicorn backend.main:app --host 0.0.0.0 --port $PORT")
