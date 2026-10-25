param([string]$ScriptPath, [string]$Scenario)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'parse_failed' }
# Load only function definitions. Never run the Windows script entrypoint.
foreach ($node in $ast.EndBlock.Statements) {
    if ($node -is [Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($node.Extent.Text))
    }
}
$script:events = [Collections.Generic.List[string]]::new()
$script:claimed = $false
$script:secretRef = $null
$script:secureLength = 0
$script:secureReadonly = $false
$script:accountEnabled = $Scenario -ne 'disabled'
function Get-AstinusSetupAccount {
    param($Label)
    $script:events.Add('context')
    if ($Scenario -eq 'context') { throw 'synthetic-sensitive-diagnostic' }
    return [pscustomobject]@{SID='S-1-5-21-1-2-3-500'; Enabled=$script:accountEnabled}
}
function New-AstinusAttempt {
    param($Nonce)
    $script:events.Add('claim')
    if ($Scenario -eq 'claim' -or $script:claimed) { throw 'synthetic-sensitive-diagnostic' }
    $script:claimed = $true
    return 'synthetic-unused-path'
}
function Set-LocalUser {
    param($SID, [Security.SecureString]$Password, $ErrorAction)
    if (-not $script:claimed -or $SID -cne 'S-1-5-21-1-2-3-500') { throw 'ordering' }
    $script:events.Add('set')
    # Never inspect or unmarshal the characters; this never updates any account.
    $script:secretRef = $Password
    $script:secureLength = $Password.Length
    $script:secureReadonly = $Password.IsReadOnly()
    if ($Scenario -eq 'set') { throw 'synthetic-sensitive-diagnostic' }
}
function Get-LocalUser {
    param($SID, $ErrorAction)
    $script:events.Add('verify')
    return [pscustomobject]@{SID=$SID; Enabled=$script:accountEnabled;
        PasswordRequired=($Scenario -ne 'verify'); PasswordLastSet=[datetime]'2026-01-01T00:00:00Z'}
}
function Write-AstinusReceipt {
    param($Root, $Nonce, $Account)
    $script:events.Add('receipt')
    if ($Scenario -eq 'receipt') { throw 'synthetic-sensitive-diagnostic' }
}
$result = Invoke-AstinusInitialization -Nonce '0123456789abcdef0123456789abcdef' -Label 'AL0123456789AB'
if ($Scenario -eq 'repeat') {
    $result = Invoke-AstinusInitialization -Nonce '0123456789abcdef0123456789abcdef' -Label 'AL0123456789AB'
}
$disposed = $false
if ($null -ne $script:secretRef) {
    try { $null = $script:secretRef.IsReadOnly() } catch [ObjectDisposedException] { $disposed = $true }
}
@{result=$result; events=@($script:events); secure_length=$script:secureLength;
  secure_readonly=$script:secureReadonly; secret_disposed=$disposed;
  account_enabled=$script:accountEnabled} | ConvertTo-Json -Compress -Depth 4
