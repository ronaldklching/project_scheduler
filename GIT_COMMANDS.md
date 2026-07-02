# Git Commands

Common commands for this project.

## Check Status

```bash
git status
```

## See What Changed

```bash
git diff
```

For one file:

```bash
git diff templates/dashboard.html
```

## Save Local Changes To Git

```bash
git add .
git commit -m "Describe the change"
```

## Upload Changes To GitHub

```bash
git push
```

## Pull Latest Changes On The Server

```bash
cd ~/Documents/project_scheduler
git pull
```

Restart the Flask app after pulling code changes.

## Discard A Local File Change

Use this only when you do not want to keep the local edit:

```bash
git restore README.md
```

Replace `README.md` with the file you want to restore.

## Typical Workflow

On your Mac:

```bash
git status
git add .
git commit -m "Update project dashboard"
git push
```

On the server:

```bash
cd ~/Documents/project_scheduler
git pull
```
