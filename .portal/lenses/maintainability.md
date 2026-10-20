+++
id = "maintainability"
title = "One module, one job"
inputs = ["diff", "files", "standards"]
required = false
version = 1
+++
Does the change keep each module to one job and respect the layers in `.importlinter`? The
library (`model`, `admission`, `elastic`, `ledger`, `quota`) never imports the daemon, and
anything that talks to a daemon goes through `leasehold.client`. Look for a module that now
does two things, an import that points up the stack, and new behaviour placed in a module
whose docstring does not cover it.
