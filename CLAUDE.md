# Working agreements

- **Always ask before commit, push, or deploy.** After making code changes, do not
  run `git commit`, `git push`, `./scripts/update_backend.sh`, or restart the Mac
  agent/backend services on your own — surface that the changes are ready and ask
  the user whether to commit/push/deploy. Only proceed once they explicitly say so
  (a yes for one round doesn't carry forward to the next).
