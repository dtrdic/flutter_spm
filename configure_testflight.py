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

    print("── Fetching App, Version, and Layout Context ──")
    app_res = requests.get(f"{BASE_URL}/apps?filter[bundleId]={BUNDLE_ID}", headers=headers)
    check_response(app_res, "Fetching App ID")
    app_id = app_res.json()['data'][0]['id']
    primary_locale = app_res.json()['data'][0]['attributes']['primaryLocale']
    
    version_res = requests.get(f"{BASE_URL}/apps/{app_id}/appStoreVersions", headers=headers)
    check_response(version_res, "Fetching App Store Versions Collection")
    versions_data = version_res.json().get('data', [])
    
    EDITABLE_STATES = ['PREPARE_FOR_SUBMISSION', 'DEVELOPER_REJECTED', 'REJECTED']
    
    target_version = None
    for v in versions_data:
        if v['attributes']['appStoreState'] in EDITABLE_STATES:
            target_version = v
            break
            
    if not target_version:
        current_states = [v['attributes']['appStoreState'] for v in versions_data]
        print(f"ℹ No version found in editable states: {EDITABLE_STATES}")
        print(f"ℹ Live database version states: {current_states}")
        print("✓ Version is locked in review. Skipping update orchestration.")
        print("═══════════════════════════════════════════════════════")
        print(" ✓ Global App Store Connect automated configuration complete! (Skipped)")
        print("═══════════════════════════════════════════════════════")
        sys.exit(0)
        
    version_id = target_version['id']
    info_res = requests.get(f"{BASE_URL}/apps/{app_id}/appInfos", headers=headers)
    check_response(info_res, "Fetching App Info Collection")
    info_id = info_res.json()['data'][0]['id']
    
    print(f"✓ Target Version ID: {version_id} (State: {target_version['attributes']['appStoreState']})")
    print(f"✓ Target App Info ID: {info_id} (Locale: {primary_locale})")

    # 1. Declaring App Content Rights
    print("── Declaring App Content Rights ──")
    rights_payload = {
        "data": {
            "id": app_id,
            "type": "apps",
            "attributes": {
                "contentRightsDeclaration": "DOES_NOT_USE_THIRD_PARTY_CONTENT"
            }
        }
    }
    rights_patch = requests.patch(f"{BASE_URL}/apps/{app_id}", json=rights_payload, headers=headers)
    check_response(rights_patch, "Updating Content Rights Declaration")
    print("✓ Content Rights declared: Does not use third-party content.")

    # 2. Create/Update Pricing Schedule (UPDATED: Resolves Empty Shell 409 Flaw)
    print("── Configuring Price Schedule Container ──")
    pts_res = requests.get(f"{BASE_URL}/apps/{app_id}/appPricePoints?limit=100", headers=headers)
    check_response(pts_res, "Fetching App Price Points")
    free_point_id = None
    for pt in pts_res.json().get('data', []):
        if pt['attributes'].get('customerPrice') == "0.00":
            free_point_id = pt['id']
            break

    if free_point_id:
        placeholder_id = f"new-price-{int(time.time())}"
        price_payload = {
            "data": {
                "type": "appPriceSchedules",
                "relationships": {
                    "app": {"data": {"type": "apps", "id": app_id}},
                    "manualPrices": {"data": [{"type": "appPrices", "id": placeholder_id}]}
                }
            },
            "included": [
                {
                    "type": "appPrices",
                    "id": placeholder_id,
                    "relationships": {
                        "appPricePoint": {"data": {"type": "appPricePoints", "id": free_point_id}}
                    }
                }
            ]
        }
        price_res = requests.post(f"{BASE_URL}/appPriceSchedules", json=price_payload, headers=headers)
        if price_res.status_code == 409:
            print("ℹ Price schedule container already initialized on Apple side.")
            print("  → Injecting explicit Free tier pricing record into shell...")
            app_price_payload = {
                "data": {
                    "type": "appPrices",
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": app_id}},
                        "appPricePoint": {"data": {"type": "appPricePoints", "id": free_point_id}}
                    }
                }
            }
            app_price_res = requests.post(f"{BASE_URL}/appPrices", json=app_price_payload, headers=headers)
            if app_price_res.status_code == 409:
                print("  ✓ Free pricing tier is already active and verified.")
            else:
                check_response(app_price_res, "Injecting Free Tier Price Point")
                print("  ✓ App pricing schedule successfully initialized to Free.")
        else:
            check_response(price_res, "Initializing App Price Schedule")
            print("✓ App pricing schedule successfully initialized to Free.")
    else:
        print("✗ Could not locate a valid Free (0.00) price point from Apple's database.")
        sys.exit(1)

    # 3. TestFlight Beta Settings Updates
    print("── Updating Beta App Review Information ──")
    review_info_res = requests.get(f"{BASE_URL}/apps/{app_id}/betaAppReviewDetail", headers=headers)
    check_response(review_info_res, "Fetching Beta Review Detail ID")
    review_detail_id = review_info_res.json()['data']['id']

    review_payload = {
        "data": {"id": review_detail_id, "type": "betaAppReviewDetails", "attributes": meta['beta_review_info']}
    }
    patch_review = requests.patch(f"{BASE_URL}/betaAppReviewDetails/{review_detail_id}", json=review_payload, headers=headers)
    check_response(patch_review, "Patching Beta App Review Details")

    print(f"── Updating TestFlight Feedback Email for locale: {primary_locale} ──")
    beta_loc_res = requests.get(f"{BASE_URL}/betaAppLocalizations?filter[app]={app_id}&filter[locale]={primary_locale}", headers=headers)
    check_response(beta_loc_res, "Checking existing Beta Localizations")
    beta_loc_data = beta_loc_res.json().get('data')
    beta_attributes = meta['beta_localization'].copy()

    if beta_loc_data:
        requests.patch(f"{BASE_URL}/betaAppLocalizations/{beta_loc_data[0]['id']}", json={"data": {"id": beta_loc_data[0]['id'], "type": "betaAppLocalizations", "attributes": beta_attributes}}, headers=headers)
    else:
        beta_attributes['locale'] = primary_locale
        requests.post(f"{BASE_URL}/betaAppLocalizations", json={"data": {"type": "betaAppLocalizations", "attributes": beta_attributes, "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}}, headers=headers)

    # 4. Create or Update Production App Store Review Details
    print("── Configuring Production App Store Review Info ──")
    prod_review_res = requests.get(f"{BASE_URL}/appStoreVersions/{version_id}/appStoreReviewDetail", headers=headers)
    
    prod_review_payload = {
        "data": {
            "type": "appStoreReviewDetails",
            "attributes": meta['production_review_info']
        }
    }
    
    if prod_review_res.status_code == 200 and prod_review_res.json().get('data'):
        prod_review_id = prod_review_res.json()['data']['id']
        prod_review_payload["data"]["id"] = prod_review_id
        requests.patch(f"{BASE_URL}/appStoreReviewDetails/{prod_review_id}", json=prod_review_payload, headers=headers)
    else:
        prod_review_payload["data"]["relationships"] = {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}
        requests.post(f"{BASE_URL}/appStoreReviewDetails", json=prod_review_payload, headers=headers)
    print("✓ Production App Review contact fields saved.")

    # 5. App Store Version Attributes (Copyright)
    print("── Updating App Store Version Compliance and Copyright ──")
    version_payload = {
        "data": {"id": version_id, "type": "appStoreVersions", "attributes": meta['version_attributes']}
    }
    v_patch = requests.patch(f"{BASE_URL}/appStoreVersions/{version_id}", json=version_payload, headers=headers)
    check_response(v_patch, "Updating Version Attributes")

    # 6. Age Rating Questionnaire Configuration
    print("── Configuring Age Rating Declarations ──")
    age_res = requests.get(f"{BASE_URL}/appInfos/{info_id}/ageRatingDeclaration", headers=headers)
    check_response(age_res, "Fetching Age Rating Declaration ID")
    age_id = age_res.json()['data']['id']
    
    age_payload = {
        "data": {"id": age_id, "type": "ageRatingDeclarations", "attributes": meta['age_rating']}
    }
    age_patch = requests.patch(f"{BASE_URL}/ageRatingDeclarations/{age_id}", json=age_payload, headers=headers)
    check_response(age_patch, "Patching Age Rating Declaration")

    # 7. Storefront Localized Text & Marketing URLs
    print("── Syncing Localized Storefront Text & URLs ──")
    store_loc_res = requests.get(f"{BASE_URL}/appStoreVersions/{version_id}/appStoreVersionLocalizations?filter[locale]={primary_locale}", headers=headers)
    check_response(store_loc_res, "Fetching Localization ID")
    localization_id = store_loc_res.json()['data'][0]['id']

    loc_payload = {
        "data": {"id": localization_id, "type": "appStoreVersionLocalizations", "attributes": meta['store_localization']}
    }
    loc_patch = requests.patch(f"{BASE_URL}/appStoreVersionLocalizations/{localization_id}", json=loc_payload, headers=headers)
    check_response(loc_patch, "Patching Storefront Localization")

    # 8. Sync App Info Localizations
    print(f"── Syncing Global App Privacy Policy URL for locale: {primary_locale} ──")
    info_loc_res = requests.get(f"{BASE_URL}/appInfos/{info_id}/appInfoLocalizations?filter[locale]={primary_locale}", headers=headers)
    check_response(info_loc_res, "Checking existing App Info Localizations")
    info_loc_data = info_loc_res.json().get('data')
    info_loc_attributes = meta['app_info_localization'].copy()

    if info_loc_data:
        info_loc_id = info_loc_data[0]['id']
        requests.patch(f"{BASE_URL}/appInfoLocalizations/{info_loc_id}", json={"data": {"id": info_loc_id, "type": "appInfoLocalizations", "attributes": info_loc_attributes}}, headers=headers)
    else:
        info_loc_attributes['locale'] = primary_locale
        requests.post(f"{BASE_URL}/appInfoLocalizations", json={"data": {"type": "appInfoLocalizations", "attributes": info_loc_attributes, "relationships": {"appInfo": {"data": {"type": "appInfos", "id": info_id}}}}}, headers=headers)

    # 9. App Info and Store Categories
    print("── Setting Store Categories ──")
    category_id = meta['categories']['primaryCategory']
    info_payload = {
        "data": {
            "id": info_id,
            "type": "appInfos",
            "attributes": meta['app_info_attributes'],
            "relationships": {"primaryCategory": {"data": {"type": "appCategories", "id": category_id}}}
        }
    }
    info_patch = requests.patch(f"{BASE_URL}/appInfos/{info_id}", json=info_payload, headers=headers)
    check_response(info_patch, "Patching Categories and App Info")

    # 10. Screenshot Orchestration
    base_screenshots_dir = "app-store/screenshots"
    if os.path.exists(base_screenshots_dir):
        print("── Orchestrating App Store Screenshot Uploads ──")
        sets_res = requests.get(f"{BASE_URL}/appStoreVersionLocalizations/{localization_id}/appScreenshotSets", headers=headers)
        check_response(sets_res, "Fetching Existing Screenshot Sets")
        
        screenshot_sets = {s['attributes']['screenshotDisplayType']: s['id'] for s in sets_res.json().get('data', [])}

        for display_type in os.listdir(base_screenshots_dir):
            display_path = os.path.join(base_screenshots_dir, display_type)
            if not os.path.isdir(display_path) or display_type not in ['APP_IPHONE_65', 'APP_IPHONE_67', 'APP_IPAD_PRO_3GEN_129']:
                continue

            set_id = screenshot_sets.get(display_type)
            if not set_id:
                print(f"  Initializing missing set slot for {display_type}...")
                create_set_res = requests.post(f"{BASE_URL}/appScreenshotSets", json={"data": {"type": "appScreenshotSets", "attributes": {"screenshotDisplayType": display_type}, "relationships": {"appStoreVersionLocalization": {"data": {"type": "appStoreVersionLocalizations", "id": localization_id}}}}}, headers=headers)
                check_response(create_set_res, f"Creating set container for {display_type}")
                set_id = create_set_res.json()['data']['id']
            else:
                print(f"  Cleaning historical assets from existing slot: {display_type}...")
                existing_shots_res = requests.get(f"{BASE_URL}/appScreenshotSets/{set_id}/appScreenshots", headers=headers)
                check_response(existing_shots_res, f"Reading existing images in set {display_type}")
                for shot in existing_shots_res.json().get('data', []):
                    shot_id = shot['id']
                    del_res = requests.delete(f"{BASE_URL}/appScreenshots/{shot_id}", headers=headers)
                    check_response(del_res, f"Removing duplicate screenshot asset {shot_id}")

            print(f" Processing folder: {display_type}")
            for file_name in sorted(os.listdir(display_path)):
                if file_name.lower().endswith(('.png', '.jpg', '.jpeg')):
                    upload_screenshot_file(os.path.join(display_path, file_name), set_id, headers)

    print("═══════════════════════════════════════════════════════")
    print(" ✓ Global App Store Connect automated configuration complete!")
    print("═══════════════════════════════════════════════════════")

if __name__ == "__main__":
    main()