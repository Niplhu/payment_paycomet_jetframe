# Prueba del endpoint /v1/form de PayComet desde PowerShell
# 1. Sustituye la API Key por la que tienes en Odoo: Contabilidad > Configuración > Proveedores de pago > Paycomet JET > API Key
# 2. Debe ser la clave de "API REST" / "API Key" del back office de PayComet (no el JET ID)

$apiKey = "REPLACE_WITH_YOUR_API_KEY"
$uri = "https://rest.paycomet.com/v1/form"

$body = @{
    operationType = 1
    terminal = 60811
    payment = @{
        terminal = 60811
        methods = @(1)
        order = "PAY987654321"
        amount = "202"
        currency = "EUR"
        secure = 1
        urlOk = "https://rich-goosewinged-annabell.ngrok-free.dev/payment/jetframe/return?reference=TEST&status=ok"
        urlKo = "https://rich-goosewinged-annabell.ngrok-free.dev/payment/jetframe/return?reference=TEST&status=ko"
    }
} | ConvertTo-Json -Depth 5 -Compress

$headers = @{
    "Accept"       = "application/json"
    "Content-Type"  = "application/json"
    "PAYCOMET-API-TOKEN" = $apiKey
}

try {
    $response = Invoke-WebRequest -Uri $uri -Method POST -Headers $headers -Body $body -UseBasicParsing
    Write-Host "OK StatusCode:" $response.StatusCode
    $response.Content | ConvertFrom-Json | ConvertTo-Json -Depth 10
} catch {
    $statusCode = $_.Exception.Response.StatusCode.value__
    Write-Host "Error HTTP:" $statusCode
    if ($_.Exception.Response) {
        $stream = $_.Exception.Response.GetResponseStream()
        if ($stream) {
            $reader = New-Object System.IO.StreamReader($stream)
            $body = $reader.ReadToEnd()
            $reader.Close()
            Write-Host "Cuerpo de respuesta:" $body
        }
    }
    Write-Host ""
    if ($statusCode -eq 401) {
        Write-Host "401 Unauthorized = API Key incorrecta o sin permiso para este terminal."
        Write-Host "Comprueba en el back office de PayComet:"
        Write-Host "  - Que la API Key sea la de 'API REST' (no el JET ID)."
        Write-Host "  - Que la API Key esté activa y asociada al terminal 50333."
        Write-Host "  - Entorno: si usas test, la URL puede ser distinta (consulta la documentación PayComet)."
    }
}
