# Sports Scores for Jarvis

Sports scores, live games, and upcoming schedules for Big 4 (NFL, NBA, MLB, NHL) and College teams via ESPN.

## Components

| Type | Name | Description |
|------|------|-------------|
| Command | `get_sports` | "How did the Giants do?", "What time is the Nets game tonight?", "Did the Lakers win last night?" |

## Install

```bash
jarvis pantry install --url https://github.com/alexberardi/jarvis-cmd-sports
```

Or from a local checkout:

```bash
jarvis pantry install --local /path/to/jarvis-cmd-sports
```

## Features

- Past game results and scores
- Live/in-progress game updates
- Upcoming game schedules (scans 7 days ahead)
- Team name fuzzy matching
- Big 4 leagues: NFL, NBA, MLB, NHL
- College teams supported

## Structure

```
jarvis_package.yaml
commands/
  get_sports/command.py
```

## License

MIT
