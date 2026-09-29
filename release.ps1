<#
.SYNOPSIS
Νέα έκδοση: bump, commit, tag και push. Το push του tag ξεκινάει το GitHub Actions build.

.DESCRIPTION
  .\release.ps1          patch: 1.5.0 -> 1.5.1  (διορθώσεις, μικρές αλλαγές)
  .\release.ps1 minor    minor: 1.5.0 -> 1.6.0  (νέα features, συμβατό με πριν)
  .\release.ps1 major    major: 1.5.0 -> 2.0.0  (μεγάλες/ασύμβατες αλλαγές)

Το working tree πρέπει να είναι καθαρό (όλα committed) πριν το τρέξεις.
#>
param([ValidateSet('patch', 'minor', 'major')][string]$Bump = 'patch')
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

if (git status --porcelain) { throw 'Working tree not clean; commit or stash first.' }

uv version --bump $Bump
$v = "v$(uv version --short)"
git commit -am "bump $v"
git tag $v
git push --atomic origin main $v
