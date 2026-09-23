# Examples

- `coding_agent_retry.py`: an agent retries a failing `pytest` 5 times, asks `what_failed`, forks, fixes it and remembers the fix. A second agent reads that memory and passes on its first try. It runs from a clone without installing anything.
