import os

try:
    import streamlit as st
    api_key = os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY")
except:
    api_key = os.getenv("OPENAI_API_KEY")
