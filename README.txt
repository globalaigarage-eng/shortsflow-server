ShortsFlow — Koyeb FFmpeg fix

The main error is:
FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'

This Dockerfile installs FFmpeg in the container.

Steps:
1. Place Dockerfile in the repository root beside server.py and requirements.txt.
2. Edit/replace the existing Dockerfile. Do not create a duplicate.
3. In Koyeb, configure the service to build with the Dockerfile, not the Python/buildpack builder.
   The /workspace/.heroku/... paths in your log suggest the current deployment is using a buildpack.
4. Commit the file and redeploy.
5. Check the deployment/build logs for a Docker build and successful FFmpeg installation.

This expects server.py and requirements.txt at the repository root.

Use this processor only for videos you own or have permission to edit/re-upload. Splitting or re-encoding does not remove copyright or guarantee permission to repost.
