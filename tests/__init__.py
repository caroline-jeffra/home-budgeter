"""Loads test environment before any app module can be imported."""

from dotenv import load_dotenv

load_dotenv(".env.test", override=True)
