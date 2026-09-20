$ErrorActionPreference = "Stop"
# Minimal installer: copy this skill into the current user's skills dir(s).
$SkillName = "basic-media-skill"
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path

$Destinations = @(
    (Join-Path $HOME ".claude\skills\$SkillName"),
    (Join-Path $HOME ".config\opencode\skills\$SkillName")
)
foreach ($dest in $Destinations) {
    New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
    Copy-Item -Path $Source -Destination $dest -Recurse -Force
    Write-Host "[OK] $Source -> $dest"
}
Write-Host "Installed $SkillName. Activate with: /$SkillName"