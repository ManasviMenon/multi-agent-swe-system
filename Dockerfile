# Reproduces this project's eval harness self-check in a clean, pinned environment:
#   docker build -t marshmallow-agents .
#   docker run --rm marshmallow-agents
# That runs eval/run_eval.py --all --gold, which applies each ticket's real merged fix to
# a fresh worktree at its own base commit and confirms all 25 resolve with no regressions.
# No API key and no network are needed at run time -- the agent phases are deliberately
# NOT part of this image, since those cost Gemini quota and are non-deterministic.

FROM python:3.12-slim

# git is not incidental here: the harness isolates every ticket in its own `git worktree`
# checked out at that ticket's base commit, so git is core runtime machinery, not tooling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies get their own layer, installed before the source is copied, so editing code
# reuses the cached install instead of reinstalling every package on each rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cloned at build time so `docker run` needs no network. Deliberately a full clone, not
# --depth 1: each ticket checks out its own historical base_commit, which a shallow clone
# would not contain.
RUN git clone https://github.com/marshmallow-code/marshmallow.git /app/.cache/marshmallow-repo

COPY . .

# The container is itself the isolation boundary, so there's no virtualenv to point at --
# the harness reads EVAL_PYTHON to find the interpreter it shells out to for pip installs
# and pytest runs.
ENV EVAL_PYTHON=/usr/local/bin/python

CMD ["python", "eval/run_eval.py", "--all", "--gold"]
