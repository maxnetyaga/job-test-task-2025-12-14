# Setup
Run ```uv sync``` to initialize venv and install python packages (or use any other tool that can process pyproject.toml).
You also need to source .envrc file in order to update PATH and load some more environment variables that used by programm.

# Usage
After setup you can use [*tmuxinator*](https://github.com/tmuxinator/tmuxinator) to start test tmux environment (launches main server with one worker per each machine core; opens 1 message send cli, 4 client panges and file with session statistics in *less* (you can hit *q* to reload less to see changes)). Or if you don't want to use tmux/tmuxinator, just open .tmuxinator.yaml and run commands from there manually.
Press C-c second time after you shutdown main process in order to show time left before forced shutdown and count of connected users per process. 

# Graceful shutdown logic
I intercept workers' SIGTERM and SIGINT signals and process them in this way:

```python
with contextlib.suppress(TimeoutError):
    await asyncio.wait_for(_wait_for_users_disconnect(), timeout)
```
```asyncio.wait_for``` waits till _wait_for_users_disconnect() finishes or timeout gets ellapsed. Then I do a bit of cleanup and stop event loop.

Here is ```_wait_for_users_disconnect```:
```python
async def _wait_for_users_disconnect():
    logger.info("len(connections)=%s", len(connections))
    while len(connections) != 0:
        await asyncio.sleep(1)
```

# Notes
Here I use multiple processes orchestration via unix sockets instead of message queues for sake of simlicity. Valkey might be more convenient here, but requires more setup. 
