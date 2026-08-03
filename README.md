# Segue Decompilation Project

## Why does this project exist?

I'm a big fan of the Forza Horizon franchise and always wanted a ban-safe app that lets me play music from Spotify or YouTube in an immersive way.

Segue is exactly that, but there was one problem: the project was never open-sourced. The creator once said he would release it, but that never happened.

With limited reverse-engineering skills, a few tools, and help from LLMs, I set out to recover the code myself. This was also a way to show that Segue is a legitimate app, with no malware or anything shady.

## How was the decompilation done? Is it the same code?

Most of the recovered code is effectively identical to the original source (aside from comments and formatting).

When a Python script runs, it is compiled into a `.pyc` file (Python bytecode) that the interpreter executes. Tools like `pydisasm` turn that bytecode into a readable form. With **pylingual**, I could get a solid picture of each module, then rewrite or paste back code when the bytecode matched.

I can claim the code matches the original because the bytecode of the decompiled version can be compared directly against the original compiled files. With LLM help on some functions, most of the project was brought back to a working state.

## What's missing, and will it be finished?

`settings.pyc` is a large file (~800 KB) that includes HTML and CSS. It will be reversed eventually, but for now the focus is on the other modules.

Some of these files are trickier to reverse, and I’ll work through them in my spare time.

Still missing:

- `overlay.pyc`
- `overlay_server.pyc`
- `runner.pyc`
- `settings.pyc`
- `wheel_hid.pyc`

***

All credit goes to the original Segue creator. I claim none of it.

Stay safe.