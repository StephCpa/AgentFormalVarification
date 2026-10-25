param([string]$InputDoc,[string]$OutputPdf)
$ErrorActionPreference = 'Stop'
$designApp = $null
$designDocument = $null
try {
  $designApp = New-Object -ComObject Word.Application
  $designApp.Visible = $false
  $designApp.DisplayAlerts = 0
  $designDocument = $designApp.Documents.Open($InputDoc, $false, $true)
  $designDocument.Repaginate()
  $designDocument.ExportAsFixedFormat($OutputPdf, 17)
  Write-Output 'PDF export completed'
} finally {
  if ($null -ne $designDocument) { try { $designDocument.Close(0) } catch { } }
  if ($null -ne $designApp) { try { $designApp.Quit() } catch { } }
}
