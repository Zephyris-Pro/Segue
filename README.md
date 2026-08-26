# Segue Decompilation Project

> [!WARNING]
> This is not the real project. The actual project can be found here: [Segueapp/segue-releases](https://github.com/Segueapp/segue-releases)
>
>> As of today (08/26/2026) it seems like Segue has gone quiet, so I’m taking the liberty of bringing the project back to life by trying to fix the bugs and potentially add some new features.

## Why does this project exist?

I'm a big fan of the Forza Horizon franchise and always wanted a ban-safe app that lets me play music from Spotify or YouTube in an immersive way.

Segue is exactly that, but there was one problem: the project was never open-sourced. The creator once said he would release it, but that never happened.

With limited reverse-engineering skills, a few tools, and help from LLMs, I set out to recover the code myself. This was also a way to show that Segue is a legitimate app, with no malware or anything shady.

## How was the decompilation done? Is it the same code?

Most of the recovered code is effectively identical to the original source (aside from comments and formatting).

When a Python script runs, it is compiled into a `.pyc` file (Python bytecode) that the interpreter executes. Tools like `pydisasm` turn that bytecode into a readable form. With **pylingual**, I could get a solid picture of each module, then rewrite or paste back code when the bytecode matched.

I can claim the code matches the original because the bytecode of the decompiled version can be compared directly against the original compiled files. With LLM help on some functions, most of the project was brought back to a working state.

## What's missing, and will it be finished?

Nothing is missing. Everything has been reverse-engineered. I’ll therefore focus on fixing bugs and potentially adding some new features.


> [!IMPORTANT]
> Despite the tests I have carried out, it is possible that the decompilation is not perfect and could still contain bugs. A broader range of tests, or people dedicated to this task, would be needed, and their help would be appreciated.
 
***

## Preview

<img width="auto" alt="preview" src="https://github.com/user-attachments/assets/3a6c42a1-7242-4b29-9272-5d334a7bce1e"/>
<br>
<br>


All credit goes to the original Segue creator. I claim none of it.

Stay safe.