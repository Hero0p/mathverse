"""Background work. Video rendering runs on an in-process thread pool
(video_queue) rather than a broker-backed worker, so the whole app is a
single `python run.py`."""
