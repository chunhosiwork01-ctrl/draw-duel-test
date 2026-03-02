# Draw Duel

Two-player online drawing duel with rooms, round prompts, AI judging, and roast commentary.

## Run

```bash
cd draw_duel
python3 server.py
```

Open:

```text
http://127.0.0.1:8765
```

## AI Judge

If `OPENAI_API_KEY` is set, the server will try to use OpenAI vision judging.
If not, it falls back to a local heuristic judge.

Optional model override:

```bash
export OPENAI_MODEL=gpt-4.1-mini
```

## Notes

- This version uses HTTP polling instead of WebSockets so it runs without extra Python packages.
- The game is real two-player room-based multiplayer, but not yet optimized for high-frequency production traffic.

## Deploy To Render

1. Push this repo to GitHub.
2. In Render, choose `New +` -> `Blueprint`.
3. Connect the GitHub repo.
4. Render will detect [`render.yaml`](/Users/sijunhao/Desktop/桌面%20-%20%E6%96%AF%E4%BF%8A%E8%B1%AA%E7%9A%84MacBook%20Air/SS1012/draw_duel/render.yaml).
5. Confirm deployment.
6. After deploy, open:
   - `https://YOUR-RENDER-SERVICE.onrender.com`

Optional AI judging:

1. Open the Render service dashboard.
2. Add environment variable `OPENAI_API_KEY`.
3. Redeploy the service.
