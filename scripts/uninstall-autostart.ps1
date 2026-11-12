# pm5bot: remove the login auto-start created by scripts\install-autostart.ps1.
$launcher = Join-Path ([Environment]::GetFolderPath("Startup")) "pm5bot.cmd"
if (Test-Path $launcher) {
  Remove-Item $launcher -Force
  Write-Host "Removed: $launcher" -ForegroundColor Green
} else {
  Write-Host "Nothing to remove: $launcher does not exist."
}
