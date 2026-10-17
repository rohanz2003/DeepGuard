"""Dependency-light research baseline for audio authenticity experiments.

This package operates on validated WAV bytes and produces safe JSON model
artifacts. The Flask backend can load a configured artifact for local research;
the decoder dependency is needed only for offline MP3 dataset preparation.
"""

__version__ = "0.1.0"
