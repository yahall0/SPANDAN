param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination
)

$ErrorActionPreference = 'Stop'
$word = $null
$document = $null
$previousLinks = $null
$dontSave = 0
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $previousLinks = $word.Options.UpdateLinksAtOpen
    $word.Options.UpdateLinksAtOpen = $false
    $confirm = $false
    $readOnly = $true
    $addToRecent = $false
    $docxFormat = 12
    $document = $word.Documents.Open([ref]$Source, [ref]$confirm, [ref]$readOnly, [ref]$addToRecent)
    $document.SaveAs2([ref]$Destination, [ref]$docxFormat)
} finally {
    if ($null -ne $document) {
        $document.Close([ref]$dontSave)
    }
    if ($null -ne $word) {
        if ($null -ne $previousLinks) {
            $word.Options.UpdateLinksAtOpen = $previousLinks
        }
        $word.Quit([ref]$dontSave)
    }
}
