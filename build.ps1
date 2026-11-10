$ErrorActionPreference = "Stop"
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onefile --name ut-coop --paths . `
  --hidden-import utcoop.puppet `
  --hidden-import utcoop.player_state `
  --hidden-import utcoop.sync_proto `
  --hidden-import utcoop.coop_bridge `
  --hidden-import utcoop.scan_rdram `
  --hidden-import utcoop.boot_to_gameplay `
  --distpath dist --workpath build client.py
Write-Host "built dist/ut-coop.exe"
