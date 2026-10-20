+++
id = "public-text"
title = "Text the public reads"
inputs = ["diff"]
required = false
version = 1
+++
This repository is public. Read the prose the change adds or edits: docs, README, CHANGELOG,
docstrings, comments, CLI help and messages.

- Does it name a private machine, host, service or project that the repository does not
  already name? Report each one.
- Does it read like a person wrote it? Flag staged contrasts ("not X but Y"), one-line closers
  that repeat the point, lists of three that the meaning does not need, inflated words, and
  em or en dashes in prose.
- Do comments and docstrings state what is guaranteed, once, in plain words, with no task
  history?

Only the prose is in scope: leave code, inline code, commands, paths and data alone.
