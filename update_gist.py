name: Update Elpris Gist

on:
  schedule:
    - cron: '10 0 * * *'  # Runs daily at 00:10 UTC
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.x'

      - name: Install dependencies
        run: pip install requests

      - name: Run elpris updater
        env:
          GIST_ID: 38a1294a14afc7b05553c2658576a721  # Replace with your actual Gist ID if different
          GITHUB_TOKEN: ${{ secrets.GH_PERSONAL_TOKEN }}
        run: python update_gist.py
