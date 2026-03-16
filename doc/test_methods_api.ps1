param(
    [Parameter(Mandatory = $true)]
    [string]$ApiKey,

    [Parameter(Mandatory = $true)]
    [int]$Terminal,

    [string]$BaseUrl = "https://rest.paycomet.com"
)

function Get-FirstMethodArray {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Object
    )

    if ($Object -is [System.Array]) {
        return $Object
    }

    if ($null -eq $Object) {
        return @()
    }

    $props = $Object.PSObject.Properties
    foreach ($prop in $props) {
        $value = $prop.Value
        if ($value -is [System.Array] -and $value.Count -gt 0) {
            $first = $value[0]
            if ($null -ne $first -and $first.PSObject -and $first.PSObject.Properties) {
                $hasId = ($first.PSObject.Properties.Name -contains 'id') -or ($first.PSObject.Properties.Name -contains 'methodId')
                if ($hasId) {
                    return $value
                }
            }
        }
    }

    return @()
}

$uri = "$($BaseUrl.TrimEnd('/'))/v1/methods"

$headers = @{
    "Accept" = "application/json"
    "Content-Type" = "application/json"
    "PAYCOMET-API-TOKEN" = $ApiKey
}

$body = @{
    terminal = $Terminal
} | ConvertTo-Json -Depth 5 -Compress

Write-Host "Consultando metodos en: $uri"
Write-Host "Terminal: $Terminal"

try {
    $response = Invoke-WebRequest -Uri $uri -Method POST -Headers $headers -Body $body -UseBasicParsing
    $rawContent = $response.Content
    $parsed = $rawContent | ConvertFrom-Json

    # Normaliza posibles formatos de respuesta
    # - array directo
    # - { methods: [...] }
    # - { result: [...] }
    # - { data: [...] }
    $methods = @()
    if ($parsed -is [System.Array]) {
        $methods = $parsed
    }
    elseif ($null -ne $parsed.methods -and $parsed.methods -is [System.Array]) {
        $methods = $parsed.methods
    }
    elseif ($null -ne $parsed.result -and $parsed.result -is [System.Array]) {
        $methods = $parsed.result
    }
    elseif ($null -ne $parsed.data -and $parsed.data -is [System.Array]) {
        $methods = $parsed.data
    }
    elseif ($null -ne $parsed) {
        # Algunos entornos devuelven objeto por id, p. ej.:
        # { "1": {...}, "33": {...} }
        $byIdValues = @()
        foreach ($prop in $parsed.PSObject.Properties) {
            $value = $prop.Value
            if ($null -eq $value) {
                continue
            }

            $valueProps = $value.PSObject.Properties.Name
            $hasId = ($valueProps -contains 'id') -or ($valueProps -contains 'methodId')
            if ($hasId) {
                $byIdValues += $value
            }
        }

        if ($byIdValues.Count -gt 0) {
            $methods = $byIdValues
        }
        else {
            $methods = @($parsed)
        }
    }

    if ($methods.Count -eq 0) {
        $discovered = Get-FirstMethodArray -Object $parsed
        if ($discovered.Count -gt 0) {
            $methods = $discovered
        }
    }

    if (-not $methods) {
        Write-Host "No se han recibido metodos para el terminal indicado."
        exit 1
    }

    Write-Host ""
    Write-Host "Metodos devueltos por Paycomet:"

    # Hay entornos donde los nombres vienen como methodId/methodName en lugar de id/name.
    $normalizedRows = foreach ($m in $methods) {
        $methodId = $null
        if ($null -ne $m.id) { $methodId = $m.id }
        elseif ($null -ne $m.methodId) { $methodId = $m.methodId }

        $methodName = $null
        if ($null -ne $m.name) { $methodName = $m.name }
        elseif ($null -ne $m.methodName) { $methodName = $m.methodName }

        $isActiveValue = $null
        if ($null -ne $m.active) { $isActiveValue = $m.active }
        elseif ($null -ne $m.enabled) { $isActiveValue = $m.enabled }

        [PSCustomObject]@{
            id = $methodId
            name = $methodName
            active = $isActiveValue
            allowAPIRefunds = $m.allowAPIRefunds
            raw = ($m | ConvertTo-Json -Depth 10 -Compress)
        }
    }

    $normalizedRows |
        Select-Object id, name, active, allowAPIRefunds |
        Sort-Object id |
        Format-Table -AutoSize

    $resolvedRows = @($normalizedRows | Where-Object { $null -ne $_.id -or $null -ne $_.name })
    if ($resolvedRows.Count -eq 0) {
        Write-Host ""
        Write-Host "No se han podido mapear columnas id/name desde la respuesta."
        Write-Host "Respuesta RAW de /v1/methods:"
        Write-Host $rawContent
        Write-Host ""
        Write-Host "Claves de primer nivel detectadas:"
        Write-Host (($parsed.PSObject.Properties.Name -join ', '))
    }

    $instantCredit = $normalizedRows | Where-Object { "$($_.id)" -eq "33" } | Select-Object -First 1

    Write-Host ""
    if (-not $instantCredit) {
        Write-Host "Instant Credit (id=33) NO aparece en /v1/methods para este terminal."
        Write-Host "Resultado: no se mostrara 'paga despues' en el formulario hospedado."
        exit 2
    }

    $isActive = @("1", "true", "True") -contains "$($instantCredit.active)"
    if ($isActive) {
        Write-Host "Instant Credit (id=33) aparece ACTIVO en este terminal."
        exit 0
    }

    Write-Host "Instant Credit (id=33) aparece pero NO esta activo (active=$($instantCredit.active))."
    Write-Host "Resultado: no se mostrara 'paga despues' hasta activarlo en Paycomet."
    Write-Host "Detalle metodo 33: $($instantCredit.raw)"
    exit 3
}
catch {
    $statusCode = $null
    try {
        $statusCode = $_.Exception.Response.StatusCode.value__
    }
    catch {
        $statusCode = "desconocido"
    }

    Write-Host "Error HTTP: $statusCode"

    if ($_.Exception.Response) {
        $stream = $_.Exception.Response.GetResponseStream()
        if ($stream) {
            $reader = New-Object System.IO.StreamReader($stream)
            $errorBody = $reader.ReadToEnd()
            $reader.Close()
            Write-Host "Cuerpo de respuesta: $errorBody"
        }
    }

    Write-Host ""
    Write-Host "Revision rapida:"
    Write-Host "- API key correcta y con permisos de consulta"
    Write-Host "- Terminal correcto (entorno test o produccion)"
    Write-Host "- Endpoint REST accesible desde esta maquina"
    exit 10
}
