import os
import sys
import time
import json
import requests
import jwt

BASE_URL = "https://api.appstoreconnect.apple.com/v1"
BUNDLE_ID = os.environ['BUNDLE_ID']

def generate_jwt():
    key_id = os.environ['APP_STORE_CONNECT_KEY_IDENTIFIER']
    issuer_id = os.environ['APP_STORE_CONNECT_ISSUER_ID']
    raw_private_key = os.environ['APP_STORE_CONNECT_PRIVATE_KEY'].strip()

    if raw_private_key.startswith("@file:"):
        file_path = raw_private_key[6:]
        print(f"── Resolving native key file path: {file_path} ──")
        with open(file_path, 'r') as f:
            raw_private_key = f.read()
            
    header = "-----BEGIN PRIVATE KEY-----"
    footer = "-----END PRIVATE KEY-----"
    clean_body = (raw_private_key.replace(header, "").replace(footer, "")
                  .replace("\\n", "").replace("\n", "").replace("\r", "").replace(" ", ""))
    wrapped_body = "\n".join(clean_body[i:i+64] for i in range(0, len(clean_body), 64))
    private_key = f"{header}\n{wrapped_body}\n{footer}\n"

    headers = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {"iss": issuer_id, "exp": int(time.time()) + 900, "aud": "appstoreconnect-v1"}
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)

def check_response(response, context_message):
    if response.status_code not in [200, 201, 204]:
        print(f"✗ ERROR DURING: {context_message}")
        print(f"Status Code: {response.status_code}")
        print(f"Apple Response: {response.text}")
        sys.exit(1)

def upload_screenshot_file(file_path, set_id, headers):
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    
    payload = {
        "data": {
            "type": "appScreenshots",
            "attributes": {"fileSize": file_size, "fileName": file_name},
            "relationships": {"appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": set_id}}}
        }
    }
    res = requests.post(f"{BASE_URL}/appScreenshots", json=payload, headers=headers)
    check_response(res, f"Reserving screenshot slot for {file_name}")
    
    screenshot_id = res.json()['data']['id']
    upload_ops = res.json()['data']['attributes']['uploadOperations']
    
    with open(file_path, 'rb') as f:
        for op in upload_ops:
            url = op['url']
            f.seek(op['offset'])
            chunk_data = f.read(op['length'])
            
            upload_headers = {h['name']: h['value'] for h in op.get('requestHeaders', [])}
            upload_headers['Content-Type'] = 'image/png'
            
            put_res = requests.put(url, data=chunk_data, headers=upload_headers)
            if put_res.status_code != 200:
                print(f"✗ Failed binary upload chunk for {file_name}")
                sys.exit(1)

    commit_payload = {
        "data": {"id": screenshot_id, "type": "appScreenshots", "attributes": {"uploaded": True}}
    }
    commit_res = requests.patch(f"{BASE_URL}/appScreenshots/{screenshot_id}", json=commit_payload, headers=headers)
    check_response(commit_res, f"Committing asset upload for {file_name}")
    print(f"  ✓ Successfully uploaded: {file_name}")

def main():
    token = generate_jwt()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with open('app-store/metadata.json') as f:
        meta = json.load(f)

    print("── Fetching Global App Information ──")
    app_res = requests.get(f"{BASE_URL}/apps?filter[bundleId]={BUNDLE_ID}", headers=headers)
    check_response(app_res, "Fetching App ID")
    app_id = app_res.json()['data'][0]['id']
    
    version_res = requests.get(f"{BASE_URL}/apps/{app_id}/appStoreVersions", headers=headers)
    check_response(version_res, "Fetching App Store Versions Collection")
    versions_data = version_res.json().get('data', [])
    
    EDITABLE_STATES = ['PREPARE_FOR_SUBMISSION', 'DEVELOPER_REJECTED', 'REJECTED']
    target_version = next((v for v in versions_data if v['attributes']['appStoreState'] in EDITABLE_STATES), None)
            
    if not target_version:
        print("ℹ No editable version found. Skipping update orchestration.")
        sys.exit(0)
        
    version_id = target_version['id']
    info_res = requests.get(f"{BASE_URL}/apps/{app_id}/appInfos", headers=headers)
    check_response(info_res, "Fetching App Info Collection")
    info_id = info_res.json()['data'][0]['id']

    # --- 1. GLOBAL SETTINGS BLOCKS ---
    print("── Updating Global Compliance, Pricing and Base Metadata ──")
    rights_res = requests.patch(f"{BASE_URL}/apps/{app_id}", json={"data": {"id": app_id, "type": "apps", "attributes": {"contentRightsDeclaration": "DOES_NOT_USE_THIRD_PARTY_CONTENT"}}}, headers=headers)
    check_response(rights_res, "Global App Content Rights Update")
    
    # Global Pricing
    pts_res = requests.get(f"{BASE_URL}/apps/{app_id}/appPricePoints?filter[territory]=USA&limit=50", headers=headers)
    check_response(pts_res, "Fetching Base Pricing Reference Points")
    free_point_id = next((pt['id'] for pt in pts_res.json().get('data', []) if pt['attributes'].get('customerPrice', '').replace(',', '.') == "0.00"), None)
    
    if free_point_id:
        local_token_id = "${newprice-0}"
        price_payload = {"data": {"type": "appPriceSchedules", "relationships": {"app": {"data": {"type": "apps", "id": app_id}}, "baseTerritory": {"data": {"type": "territories", "id": "USA"}}, "manualPrices": {"data": [{"type": "appPrices", "id": local_token_id}]}}}, "included": [{"type": "appPrices", "id": local_token_id, "attributes": {"startDate": None}, "relationships": {"appPricePoint": {"data": {"type": "appPricePoints", "id": free_point_id}}}}] }
        price_res = requests.post(f"{BASE_URL}/appPriceSchedules", json=price_payload, headers=headers)
        if price_res.status_code != 409:
            check_response(price_res, "Setting Global Pricing Tiers")

    # Beta Review Info Setup
    review_info_res = requests.get(f"{BASE_URL}/apps/{app_id}/betaAppReviewDetail", headers=headers)
    check_response(review_info_res, "Fetching Beta App Review Base Container")
    beta_patch = requests.patch(f"{BASE_URL}/betaAppReviewDetails/{review_info_res.json()['data']['id']}", json={"data": {"id": review_info_res.json()['data']['id'], "type": "betaAppReviewDetails", "attributes": meta['global']['beta_review_info']}}, headers=headers)
    check_response(beta_patch, "Updating Beta App Review Properties")
    
    # Production Review Info Setup
    prod_review_res = requests.get(f"{BASE_URL}/appStoreVersions/{version_id}/appStoreReviewDetail", headers=headers)
    if prod_review_res.status_code == 200 and prod_review_res.json().get('data'):
        prod_patch = requests.patch(f"{BASE_URL}/appStoreReviewDetails/{prod_review_res.json()['data']['id']}", json={"data": {"id": prod_review_res.json()['data']['id'], "type": "appStoreReviewDetails", "attributes": meta['global']['production_review_info']}}, headers=headers)
        check_response(prod_patch, "Patching Production Review Fields")
    else:
        prod_post = requests.post(f"{BASE_URL}/appStoreReviewDetails", json={"data": {"type": "appStoreReviewDetails", "attributes": meta['global']['production_review_info'], "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}}, headers=headers)
        check_response(prod_post, "Initializing Production Review Container")

    # App Store Version Attributes (Copyright)
    v_attr_res = requests.patch(f"{BASE_URL}/appStoreVersions/{version_id}", json={"data": {"id": version_id, "type": "appStoreVersions", "attributes": meta['global']['version_attributes']}}, headers=headers)
    check_response(v_attr_res, "Updating Legal Copyright Fields")
    
    # Age Rating Survey Processing
    age_res = requests.get(f"{BASE_URL}/appInfos/{info_id}/ageRatingDeclaration", headers=headers)
    check_response(age_res, "Fetching Age Rating Declaration Container")
    age_patch = requests.patch(f"{BASE_URL}/ageRatingDeclarations/{age_res.json()['data']['id']}", json={"data": {"id": age_res.json()['data']['id'], "type": "ageRatingDeclarations", "attributes": meta['global']['age_rating']}}, headers=headers)
    check_response(age_patch, "Submitting Age Rating Declaration Questionnaire")

    # Primary Category Configuration
    cat_res = requests.patch(f"{BASE_URL}/appInfos/{info_id}", json={"data": {"id": info_id, "type": "appInfos", "relationships": {"primaryCategory": {"data": {"type": "appCategories", "id": meta['global']['categories']['primaryCategory']}}}}}, headers=headers)
    check_response(cat_res, "Setting Primary App Store Category")

    # --- 2. DYNAMIC MULTI-LANGUAGE ORCHESTRATION LOOP ---
    for locale, data in meta['localizations'].items():
        print(f"\n🌍 Processing Localization Loop for Target Locale: [{locale}] ──")

        v_loc_res = requests.get(f"{BASE_URL}/appStoreVersions/{version_id}/appStoreVersionLocalizations?filter[locale]={locale}", headers=headers)
        check_response(v_loc_res, f"Fetching Version Localization for {locale}")
        v_loc_data = v_loc_res.json().get('data')
        
        v_loc_attributes = {
            "description": data.get("description"),
            "keywords": data.get("keywords"),
            "supportUrl": data.get("supportUrl")
        }
        
        if v_loc_data:
            v_loc_id = v_loc_data[0]['id']
            patch_res = requests.patch(f"{BASE_URL}/appStoreVersionLocalizations/{v_loc_id}", json={"data": {"id": v_loc_id, "type": "appStoreVersionLocalizations", "attributes": v_loc_attributes}}, headers=headers)
            check_response(patch_res, f"Patching Version Localization text fields for {locale}")
        else:
            v_loc_attributes["locale"] = locale
            v_post = requests.post(f"{BASE_URL}/appStoreVersionLocalizations", json={"data": {"type": "appStoreVersionLocalizations", "attributes": v_loc_attributes, "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}}, headers=headers)
            check_response(v_post, f"Creating baseline Version Localization container for {locale}")
            v_loc_id = v_post.json()['data']['id']

        info_loc_res = requests.get(f"{BASE_URL}/appInfos/{info_id}/appInfoLocalizations?filter[locale]={locale}", headers=headers)
        check_response(info_loc_res, f"Checking Info Localizations for {locale}")
        info_loc_data = info_loc_res.json().get('data')
        
        info_attributes = {
            "name": data.get("name"),
            "subtitle": data.get("subtitle"),
            "privacyPolicyUrl": data.get("privacyPolicyUrl")
        }
        
        if info_loc_data:
            info_loc_id = info_loc_data[0]['id']
            info_res = requests.patch(f"{BASE_URL}/appInfoLocalizations/{info_loc_id}", json={"data": {"id": info_loc_id, "type": "appInfoLocalizations", "attributes": info_attributes}}, headers=headers)
            
            if info_res.status_code == 409:
                print(f"⚠️ App Name '{data.get('name')}' is locked or already in use. Isolating and force-saving Privacy Policy URL...")
                info_res = requests.patch(f"{BASE_URL}/appInfoLocalizations/{info_loc_id}", json={"data": {"id": info_loc_id, "type": "appInfoLocalizations", "attributes": {"privacyPolicyUrl": data.get("privacyPolicyUrl")}}}, headers=headers)
            check_response(info_res, f"Syncing App Info details for {locale}")
        else:
            info_attributes["locale"] = locale
            info_res = requests.post(f"{BASE_URL}/appInfoLocalizations", json={"data": {"type": "appInfoLocalizations", "attributes": info_attributes, "relationships": {"appInfo": {"data": {"type": "appInfos", "id": info_id}}}}}, headers=headers)
            
            if info_res.status_code == 409:
                print(f"⚠️ App Name target taken on creation block. Initializing tracking container via privacy compliance fallback...")
                info_res = requests.post(f"{BASE_URL}/appInfoLocalizations", json={"data": {"type": "appInfoLocalizations", "attributes": {"locale": locale, "privacyPolicyUrl": data.get("privacyPolicyUrl")}, "relationships": {"appInfo": {"data": {"type": "appInfos", "id": info_id}}}}}, headers=headers)
            check_response(info_res, f"Creating App Info data fields for {locale}")

        locale_screenshot_dir = f"app-store/screenshots/{locale}"
        if os.path.exists(locale_screenshot_dir):
            print(f" 📸 Processing screenshot folder patterns for [{locale}]...")
            sets_res = requests.get(f"{BASE_URL}/appStoreVersionLocalizations/{v_loc_id}/appScreenshotSets", headers=headers)
            check_response(sets_res, f"Reading display screenshot sets for {locale}")
            screenshot_sets = {s['attributes']['screenshotDisplayType']: s['id'] for s in sets_res.json().get('data', [])}

            for display_type in os.listdir(locale_screenshot_dir):
                display_path = os.path.join(locale_screenshot_dir, display_type)
                if not os.path.isdir(display_path) or display_type not in ['APP_IPHONE_65', 'APP_IPHONE_67', 'APP_IPAD_PRO_3GEN_129']:
                    continue

                set_id = screenshot_sets.get(display_type)
                if not set_id:
                    create_set_res = requests.post(f"{BASE_URL}/appScreenshotSets", json={"data": {"type": "appScreenshotSets", "attributes": {"screenshotDisplayType": display_type}, "relationships": {"appStoreVersionLocalization": {"data": {"type": "appStoreVersionLocalizations", "id": v_loc_id}}}}}, headers=headers)
                    check_response(create_set_res, f"Creating display container for {display_type} ({locale})")
                    set_id = create_set_res.json()['data']['id']
                else:
                    existing_shots_res = requests.get(f"{BASE_URL}/appScreenshotSets/{set_id}/appScreenshots", headers=headers)
                    check_response(existing_shots_res, f"Reading screenshots inside container {display_type} ({locale})")
                    for shot in existing_shots_res.json().get('data', []):
                        del_res = requests.delete(f"{BASE_URL}/appScreenshots/{shot['id']}", headers=headers)
                        check_response(del_res, f"Clearing old screenshot file {shot['id']}")

                for file_name in sorted(os.listdir(display_path)):
                    if file_name.lower().endswith(('.png', '.jpg', '.jpeg')):
                        upload_screenshot_file(os.path.join(display_path, file_name), set_id, headers)

    print("\n── Pausing for a 45-second cooling period ──")
    time.sleep(45)
    print("═══════════════════════════════════════════════════════")
    print(" ✓ Global App Store Connect automated configuration complete!")
    print("═══════════════════════════════════════════════════════")

if __name__ == "__main__":
    main()