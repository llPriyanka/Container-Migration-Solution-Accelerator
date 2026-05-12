#!/bin/bash

# =============================================================================
# configure_auth.sh
#
# Automates Microsoft Entra ID (Azure AD) authentication setup for the
# Container Migration Solution Accelerator.
#
# This script:
#   1. Creates App Registrations for the Web (frontend) and API (backend) apps
#   2. Exposes a user_impersonation scope on each registration
#   3. Creates client secrets
#   4. Grants API permissions from the Web app to the API app
#   5. Adds the Web app as an allowed client application on the API
#   6. Configures the Microsoft identity provider on both Container Apps
#   7. Adds a SPA redirect URI to the Web app registration
#   8. Updates frontend Container App environment variables (MSAL + ENABLE_AUTH)
#
# Prerequisites:
#   - Azure CLI (az) installed and logged in
#   - azd environment provisioned (azd env get-values must return outputs)
#   - Permissions to create App Registrations and manage Container Apps
# =============================================================================

set -euo pipefail

###############################################################################
# Colours / helpers
###############################################################################
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No colour

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

###############################################################################
# Load environment values – prefer azd, fall back to resource-group query
###############################################################################
RG_ARG="${1:-}"

load_from_azd() {
    info "Loading azd environment values..."

    # Resolve project root (where .azure/ lives) regardless of where the script is invoked from
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local project_root
    project_root="$(cd "$script_dir/../.." && pwd)"

    if ! azd_values=$(cd "$project_root" && azd env get-values 2>/dev/null); then
        return 1
    fi

    get_azd_value() {
        local key="$1"
        local val
        val=$(echo "$azd_values" | grep "^${key}=" | head -1 | sed "s/^${key}=\"\?\(.*\)\"\?$/\1/" | tr -d '"')
        echo "$val"
    }

    SUBSCRIPTION_ID=$(get_azd_value "AZURE_SUBSCRIPTION_ID")
    RESOURCE_GROUP=$(get_azd_value "AZURE_RESOURCE_GROUP")
    WEB_APP_NAME=$(get_azd_value "CONTAINER_WEB_APP_NAME")
    API_APP_NAME=$(get_azd_value "CONTAINER_API_APP_NAME")
    WEB_APP_FQDN=$(get_azd_value "CONTAINER_WEB_APP_FQDN")
    API_APP_FQDN=$(get_azd_value "CONTAINER_API_APP_FQDN")

    # Fallback: try CONTAINER_FRONTEND_APP_NAME (azure_custom.yaml uses this)
    if [ -z "$WEB_APP_NAME" ]; then
        WEB_APP_NAME=$(get_azd_value "CONTAINER_FRONTEND_APP_NAME")
    fi
    if [ -z "$WEB_APP_FQDN" ]; then
        WEB_APP_FQDN=$(get_azd_value "CONTAINER_FRONTEND_APP_FQDN")
    fi

    # Return success only if we got the critical values
    [ -n "$SUBSCRIPTION_ID" ] && [ -n "$RESOURCE_GROUP" ] && [ -n "$WEB_APP_NAME" ] && [ -n "$API_APP_NAME" ]
}

load_from_resource_group() {
    local rg="$1"
    info "Fetching details from resource group: $rg"

    SUBSCRIPTION_ID=$(az account show --query id -o tsv)
    RESOURCE_GROUP="$rg"

    # Verify the resource group exists
    az group show --name "$rg" > /dev/null 2>&1 \
        || fail "Resource group '$rg' not found in subscription $SUBSCRIPTION_ID."

    # List all container apps in the resource group
    local app_list
    app_list=$(az containerapp list --resource-group "$rg" --query "[].{name:name, fqdn:properties.configuration.ingress.fqdn}" -o json 2>/dev/null)

    local app_count
    app_count=$(echo "$app_list" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null \
                || echo "$app_list" | python -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)

    [ "$app_count" -ge 2 ] 2>/dev/null \
        || fail "Expected at least 2 container apps in '$rg', found ${app_count:-0}."

    # Identify the frontend/web app and the API/backend app by name patterns
    # Patterns: "frontend", "web" → frontend app; "api", "backend" → API app
    WEB_APP_NAME=$(echo "$app_list" | python3 -c "
import sys, json
apps = json.load(sys.stdin)
for a in apps:
    n = a['name'].lower()
    if 'frontend' in n or 'web' in n:
        print(a['name']); break
" 2>/dev/null || true)

    API_APP_NAME=$(echo "$app_list" | python3 -c "
import sys, json
apps = json.load(sys.stdin)
for a in apps:
    n = a['name'].lower()
    if 'api' in n or 'backend' in n:
        print(a['name']); break
" 2>/dev/null || true)

    [ -n "$WEB_APP_NAME" ] || fail "Could not identify frontend/web container app in '$rg'. Expected a name containing 'frontend' or 'web'."
    [ -n "$API_APP_NAME" ] || fail "Could not identify API/backend container app in '$rg'. Expected a name containing 'api' or 'backend'."

    # Fetch FQDNs
    WEB_APP_FQDN=$(az containerapp show --name "$WEB_APP_NAME" --resource-group "$rg" --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null)
    API_APP_FQDN=$(az containerapp show --name "$API_APP_NAME" --resource-group "$rg" --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null)

    [ -n "$WEB_APP_FQDN" ] || fail "Could not get FQDN for container app '$WEB_APP_NAME'."
    [ -n "$API_APP_FQDN" ] || fail "Could not get FQDN for container app '$API_APP_NAME'."

    ok "Discovered Web app : $WEB_APP_NAME ($WEB_APP_FQDN)"
    ok "Discovered API app : $API_APP_NAME ($API_APP_FQDN)"
}

# If a resource group argument is provided, use it directly; otherwise try azd
if [ -n "$RG_ARG" ]; then
    info "Resource group argument provided – querying Azure directly."
    load_from_resource_group "$RG_ARG"
elif ! load_from_azd; then
    fail "Could not load azd environment and no resource group provided.\nUsage: $0 [<resource-group-name>]"
fi

# Validate required values
[ -z "$SUBSCRIPTION_ID" ] && fail "AZURE_SUBSCRIPTION_ID could not be determined."
[ -z "$RESOURCE_GROUP" ]  && fail "AZURE_RESOURCE_GROUP could not be determined."
[ -z "$WEB_APP_NAME" ]    && fail "Frontend/Web container app name could not be determined."
[ -z "$API_APP_NAME" ]    && fail "API/Backend container app name could not be determined."
[ -z "$WEB_APP_FQDN" ]    && fail "Frontend/Web container app FQDN could not be determined."
[ -z "$API_APP_FQDN" ]    && fail "API/Backend container app FQDN could not be determined."

WEB_APP_URL="https://${WEB_APP_FQDN}"
API_APP_URL="https://${API_APP_FQDN}"
TENANT_ID=$(az account show --query tenantId -o tsv)

info "Subscription : $SUBSCRIPTION_ID"
info "Resource Group: $RESOURCE_GROUP"
info "Web App       : $WEB_APP_NAME ($WEB_APP_URL)"
info "API App       : $API_APP_NAME ($API_APP_URL)"
info "Tenant ID     : $TENANT_ID"
echo ""

###############################################################################
# Helper – idempotent app registration creation
###############################################################################
create_or_get_app() {
    local display_name="$1"
    local app_id

    app_id=$(az ad app list --display-name "$display_name" --query "[0].appId" -o tsv 2>/dev/null || true)
    if [ -n "$app_id" ] && [ "$app_id" != "None" ]; then
        warn "App registration '$display_name' already exists (appId=$app_id). Reusing." >&2
    else
        info "Creating app registration: $display_name" >&2
        app_id=$(az ad app create --display-name "$display_name" --sign-in-audience AzureADMyOrg --query appId -o tsv)
        ok "Created app registration: $display_name (appId=$app_id)" >&2
    fi
    echo "$app_id"
}

###############################################################################
# Helper – add user_impersonation scope (idempotent)
###############################################################################
add_scope() {
    local app_id="$1"
    local display_name="$2"
    local scope_name="user_impersonation"
    local app_uri="api://${app_id}"

    # Set the Application ID URI if not already set
    local existing_uri
    existing_uri=$(az ad app show --id "$app_id" --query "identifierUris[0]" -o tsv 2>/dev/null || true)
    if [ -z "$existing_uri" ] || [ "$existing_uri" == "None" ]; then
        az ad app update --id "$app_id" --identifier-uris "$app_uri" 2>/dev/null
        info "Set Application ID URI to $app_uri" >&2
    else
        app_uri="$existing_uri"
        info "Application ID URI already set: $app_uri" >&2
    fi

    # Check if scope already exists
    local existing_scope
    existing_scope=$(az ad app show --id "$app_id" --query "api.oauth2PermissionScopes[?value=='${scope_name}'].id" -o tsv 2>/dev/null || true)
    if [ -n "$existing_scope" ] && [ "$existing_scope" != "None" ]; then
        info "Scope '$scope_name' already exists on $display_name" >&2
    else
        local scope_id
        scope_id=$(python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || python -c "import uuid; print(uuid.uuid4())" 2>/dev/null || powershell.exe -NoProfile -Command "[guid]::NewGuid().ToString()" 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null)
        info "Adding scope '$scope_name' to $display_name" >&2
        az ad app update --id "$app_id" --set api="{\"oauth2PermissionScopes\":[{\"id\":\"${scope_id}\",\"adminConsentDescription\":\"Allow the application to access ${display_name} on behalf of the signed-in user.\",\"adminConsentDisplayName\":\"Access ${display_name}\",\"isEnabled\":true,\"type\":\"User\",\"userConsentDescription\":\"Allow the application to access ${display_name} on your behalf.\",\"userConsentDisplayName\":\"Access ${display_name}\",\"value\":\"${scope_name}\"}]}"
        ok "Added scope: $scope_name" >&2
    fi

    # Return the full scope string
    echo "${app_uri}/${scope_name}"
}

###############################################################################
# Helper – create client secret (always creates a new one)
###############################################################################
create_secret() {
    local app_id="$1"
    local display_name="$2"
    info "Creating client secret for $display_name..." >&2
    local secret
    secret=$(az ad app credential reset --id "$app_id" --display-name "configure_auth" --years 1 --query password -o tsv)
    ok "Created client secret for $display_name" >&2
    echo "$secret"
}

###############################################################################
# Step 1 – Create App Registrations
###############################################################################
echo ""
info "========== Step 1: Create App Registrations =========="

WEB_REG_NAME="${WEB_APP_NAME}"
API_REG_NAME="${API_APP_NAME}"

WEB_CLIENT_ID=$(create_or_get_app "$WEB_REG_NAME")
API_CLIENT_ID=$(create_or_get_app "$API_REG_NAME")

ok "Web Client ID: $WEB_CLIENT_ID"
ok "API Client ID: $API_CLIENT_ID"

###############################################################################
# Step 2 – Expose API scopes (user_impersonation)
###############################################################################
echo ""
info "========== Step 2: Expose API Scopes =========="

WEB_SCOPE=$(add_scope "$WEB_CLIENT_ID" "$WEB_REG_NAME")
API_SCOPE=$(add_scope "$API_CLIENT_ID" "$API_REG_NAME")

ok "Web Scope: $WEB_SCOPE"
ok "API Scope: $API_SCOPE"

###############################################################################
# Step 3 – Create Client Secrets
###############################################################################
echo ""
info "========== Step 3: Create Client Secrets =========="

WEB_SECRET=$(create_secret "$WEB_CLIENT_ID" "$WEB_REG_NAME")
API_SECRET=$(create_secret "$API_CLIENT_ID" "$API_REG_NAME")

###############################################################################
# Step 4 – Grant API permissions (Web → API)
###############################################################################
echo ""
info "========== Step 4: Grant API Permissions =========="

# Get the API scope ID
API_SCOPE_ID=$(az ad app show --id "$API_CLIENT_ID" --query "api.oauth2PermissionScopes[?value=='user_impersonation'].id" -o tsv)

if [ -z "$API_SCOPE_ID" ] || [ "$API_SCOPE_ID" == "None" ]; then
    fail "Could not find user_impersonation scope on API app registration."
fi

# Check if permission already exists
EXISTING_PERM=$(az ad app show --id "$WEB_CLIENT_ID" --query "requiredResourceAccess[?resourceAppId=='${API_CLIENT_ID}'].resourceAccess[0].id" -o tsv 2>/dev/null || true)
if [ -n "$EXISTING_PERM" ] && [ "$EXISTING_PERM" != "None" ]; then
    info "API permission already granted from Web to API app."
else
    info "Adding API permission: Web app → API app (user_impersonation)"
    az ad app permission add --id "$WEB_CLIENT_ID" --api "$API_CLIENT_ID" --api-permissions "${API_SCOPE_ID}=Scope"
    ok "API permission added."
fi

# Grant admin consent
info "Granting admin consent..."
# Create service principal for API if it doesn't exist
az ad sp show --id "$API_CLIENT_ID" > /dev/null 2>&1 || az ad sp create --id "$API_CLIENT_ID" > /dev/null 2>&1
az ad sp show --id "$WEB_CLIENT_ID" > /dev/null 2>&1 || az ad sp create --id "$WEB_CLIENT_ID" > /dev/null 2>&1

az ad app permission grant --id "$WEB_CLIENT_ID" --api "$API_CLIENT_ID" --scope "user_impersonation" 2>/dev/null || warn "Admin consent may require tenant admin approval."

###############################################################################
# Step 5 – Add Web as allowed client application on API
###############################################################################
echo ""
info "========== Step 5: Add Web as Allowed Client on API =========="

# Get existing pre-authorized apps
EXISTING_PREAUTH=$(az ad app show --id "$API_CLIENT_ID" --query "api.preAuthorizedApplications[?appId=='${WEB_CLIENT_ID}'].appId" -o tsv 2>/dev/null || true)
if [ -n "$EXISTING_PREAUTH" ] && [ "$EXISTING_PREAUTH" != "None" ]; then
    info "Web app already pre-authorized on API app."
else
    info "Adding Web app ($WEB_CLIENT_ID) as pre-authorized client on API app..."
    # Use Graph API directly — az ad app update --set doesn't support nested api.preAuthorizedApplications
    API_OBJECT_ID=$(az ad app show --id "$API_CLIENT_ID" --query "id" -o tsv)
    az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/${API_OBJECT_ID}" \
        --headers "Content-Type=application/json" \
        --body "{\"api\":{\"preAuthorizedApplications\":[{\"appId\":\"${WEB_CLIENT_ID}\",\"delegatedPermissionIds\":[\"${API_SCOPE_ID}\"]}]}}"
    ok "Web app added as pre-authorized client."
fi

###############################################################################
# Step 6 – Add SPA redirect URI to Web app registration
###############################################################################
echo ""
info "========== Step 6: Configure Redirect URIs =========="

# Add the SPA redirect URI
info "Adding SPA redirect URI: $WEB_APP_URL"
az ad app update --id "$WEB_CLIENT_ID" \
    --set "spa={\"redirectUris\":[\"${WEB_APP_URL}\",\"${WEB_APP_URL}/\"]}"
ok "SPA redirect URIs configured."

# Add Web redirect URI for EasyAuth callback
WEB_CALLBACK="${WEB_APP_URL}/.auth/login/aad/callback"
API_CALLBACK="${API_APP_URL}/.auth/login/aad/callback"

info "Adding Web redirect URI for EasyAuth: $WEB_CALLBACK"
az ad app update --id "$WEB_CLIENT_ID" \
    --set "web={\"redirectUris\":[\"${WEB_CALLBACK}\"],\"implicitGrantSettings\":{\"enableIdTokenIssuance\":true}}"
ok "Web EasyAuth redirect URI configured."

info "Adding Web redirect URI for API EasyAuth: $API_CALLBACK"
az ad app update --id "$API_CLIENT_ID" \
    --set "web={\"redirectUris\":[\"${API_CALLBACK}\"],\"implicitGrantSettings\":{\"enableIdTokenIssuance\":true}}"
ok "API EasyAuth redirect URI configured."

###############################################################################
# Step 7 – Configure Microsoft Identity Provider on Container Apps
###############################################################################
echo ""
info "========== Step 7: Configure Auth on Container Apps =========="

# Use v2.0 issuer to match MSAL v2 tokens (NOT sts.windows.net which is v1)
ISSUER_URL="https://login.microsoftonline.com/${TENANT_ID}/v2.0"

# Configure auth on Web Container App
info "Configuring authentication on Web Container App: $WEB_APP_NAME"
az containerapp auth microsoft update \
    --name "$WEB_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --client-id "$WEB_CLIENT_ID" \
    --client-secret "$WEB_SECRET" \
    --issuer "$ISSUER_URL" \
    --allowed-audiences "api://${WEB_CLIENT_ID},${WEB_CLIENT_ID}" \
    --yes || warn "Could not configure auth on Web app. You may need to do this manually."

# Enable auth and set unauthenticated access to allow (let MSAL handle auth in the SPA)
az containerapp auth update \
    --name "$WEB_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --enabled true \
    --unauthenticated-client-action AllowAnonymous || true

ok "Web Container App auth configured."

# Configure auth on API Container App
info "Configuring authentication on API Container App: $API_APP_NAME"
az containerapp auth microsoft update \
    --name "$API_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --client-id "$API_CLIENT_ID" \
    --client-secret "$API_SECRET" \
    --issuer "$ISSUER_URL" \
    --allowed-audiences "api://${API_CLIENT_ID},${API_CLIENT_ID}" \
    --yes || warn "Could not configure auth on API app. You may need to do this manually."

# Enable auth and set unauthenticated access to return 401
az containerapp auth update \
    --name "$API_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --enabled true \
    --unauthenticated-client-action Return401 || true

# Add Web app client ID to the API's allowed applications policy
# (az CLI doesn't expose this flag — use REST API directly)
info "Adding Web app to API's allowed applications policy..."
API_RESOURCE_ID=$(az containerapp show --name "$API_APP_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)
MSYS_NO_PATHCONV=1 az rest --method PUT \
    --url "https://management.azure.com${API_RESOURCE_ID}/authConfigs/Current?api-version=2024-03-01" \
    --body "{\"properties\":{\"platform\":{\"enabled\":true},\"globalValidation\":{\"unauthenticatedClientAction\":\"Return401\"},\"identityProviders\":{\"azureActiveDirectory\":{\"registration\":{\"clientId\":\"${API_CLIENT_ID}\",\"clientSecretSettingName\":\"microsoft-provider-authentication-secret\",\"openIdIssuer\":\"${ISSUER_URL}\"},\"validation\":{\"allowedAudiences\":[\"api://${API_CLIENT_ID}\",\"${API_CLIENT_ID}\"],\"defaultAuthorizationPolicy\":{\"allowedApplications\":[\"${WEB_CLIENT_ID}\"]}}}},\"login\":{\"preserveUrlFragmentsForLogins\":false}}}" \
    || warn "Could not update allowed applications on API. You may need to do this manually."
ok "API allowed applications policy updated."

ok "API Container App auth configured."

###############################################################################
# Step 8 – Update frontend Container App environment variables
###############################################################################
echo ""
info "========== Step 8: Update Frontend Environment Variables =========="

AUTHORITY="https://login.microsoftonline.com/${TENANT_ID}"

info "Updating environment variables on $WEB_APP_NAME..."
# MSYS_NO_PATHCONV=1 prevents Git Bash from converting "/" to "C:/Program Files/Git/"
MSYS_NO_PATHCONV=1 az containerapp update \
    --name "$WEB_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --set-env-vars \
        "REACT_APP_MSAL_AUTH_CLIENTID=${WEB_CLIENT_ID}" \
        "REACT_APP_MSAL_AUTH_AUTHORITY=${AUTHORITY}" \
        "REACT_APP_MSAL_REDIRECT_URL=/" \
        "REACT_APP_MSAL_POST_REDIRECT_URL=/" \
        "REACT_APP_WEB_SCOPE=${WEB_SCOPE}" \
        "REACT_APP_API_SCOPE=${API_SCOPE}" \
        "ENABLE_AUTH=true"

ok "Frontend environment variables updated."

###############################################################################
# Summary
###############################################################################
echo ""
echo "============================================================"
echo -e "${GREEN}Authentication configuration complete!${NC}"
echo "============================================================"
echo ""
echo "Web App Registration:"
echo "  Name      : $WEB_REG_NAME"
echo "  Client ID : $WEB_CLIENT_ID"
echo "  Scope     : $WEB_SCOPE"
echo ""
echo "API App Registration:"
echo "  Name      : $API_REG_NAME"
echo "  Client ID : $API_CLIENT_ID"
echo "  Scope     : $API_SCOPE"
echo ""
echo "Frontend Container App:"
echo "  REACT_APP_MSAL_AUTH_CLIENTID   = $WEB_CLIENT_ID"
echo "  REACT_APP_MSAL_AUTH_AUTHORITY  = $AUTHORITY"
echo "  REACT_APP_WEB_SCOPE            = $WEB_SCOPE"
echo "  REACT_APP_API_SCOPE            = $API_SCOPE"
echo "  ENABLE_AUTH                    = true"
echo ""
echo "Portal Links:"
echo "  Web App: https://portal.azure.com/#resource/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.App/containerApps/${WEB_APP_NAME}"
echo "  API App: https://portal.azure.com/#resource/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.App/containerApps/${API_APP_NAME}"
echo ""
echo -e "${YELLOW}Note: It may take a few minutes for the new revision to become active.${NC}"
echo -e "${YELLOW}Note: If admin consent was not granted automatically, ask your tenant admin to approve.${NC}"
