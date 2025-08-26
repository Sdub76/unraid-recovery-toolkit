#!/usr/bin/env python3
"""
Radarr Collection Path Update Script (HTTP Requests Version with Filtering)

This script updates Radarr collection root folder paths from /movies/... to /media/movies/...
It uses direct HTTP requests to the Radarr API without requiring additional libraries.

NEW: Supports collection filtering with wildcards for selective updates!

Requirements:
    - Python 3.6+ (uses only standard library)

Usage:
    1. Set your Radarr API key and host details below
    2. Optionally set COLLECTION_FILTER for selective updates
    3. Run: python radarr_collection_path_update_filtered.py
    4. The script will show a preview first, then ask for confirmation

Author: Claude (Anthropic)
Date: 2025-08-22
"""

import json
import sys
import urllib.request
import urllib.parse
import urllib.error
import fnmatch
from typing import List, Dict, Optional

# Configuration - Update these values for your setup
RADARR_HOST = "https://radarr.waun.net"  # Update with your Radarr URL
RADARR_API_KEY = "b2aec3d260ea43d985558f41047efa0b"        # Update with your actual API key

# Path transformation settings
OLD_PATH_PREFIX = "/movies/Library"
NEW_PATH_PREFIX = "/media/movies"

# Collection filtering (NEW FEATURE!)
# Set to None to process all collections, or use wildcards to filter
# Examples:
#   COLLECTION_FILTER = None                    # Process all collections
#   COLLECTION_FILTER = "Marvel*"               # Only Marvel collections
#   COLLECTION_FILTER = "*Marvel*"             # Collections containing "Marvel"
#   COLLECTION_FILTER = ["Marvel*", "DC*"]     # Multiple patterns
#   COLLECTION_FILTER = "Fast & Furious*"      # Specific franchise
COLLECTION_FILTER = None

def make_radarr_request(endpoint: str, method: str = "GET", data: Optional[Dict] = None) -> Optional[Dict]:
    """Make an HTTP request to the Radarr API."""
    url = f"{RADARR_HOST}/api/v3/{endpoint}"
    
    headers = {
        "X-Api-Key": RADARR_API_KEY,
        "Content-Type": "application/json"
    }
    
    request_data = None
    if data:
        request_data = json.dumps(data).encode('utf-8')
    
    try:
        req = urllib.request.Request(url, data=request_data, headers=headers, method=method)
        
        with urllib.request.urlopen(req) as response:
            if response.status >= 200 and response.status < 300:
                response_data = response.read().decode('utf-8')
                return json.loads(response_data) if response_data else None
            else:
                print(f"❌ HTTP Error {response.status}: {response.reason}")
                return None
                
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP Error {e.code}: {e.reason}")
        if e.code == 401:
            print("   Check your API key - it may be incorrect")
        return None
    except urllib.error.URLError as e:
        print(f"❌ Connection Error: {e.reason}")
        print(f"   Check that Radarr is running at: {RADARR_HOST}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None

def matches_filter(collection_title: str, filter_patterns) -> bool:
    """Check if collection title matches any of the filter patterns."""
    if filter_patterns is None:
        return True
    
    # Convert single pattern to list
    if isinstance(filter_patterns, str):
        filter_patterns = [filter_patterns]
    
    # Check against each pattern
    for pattern in filter_patterns:
        if fnmatch.fnmatch(collection_title.lower(), pattern.lower()):
            return True
    
    return False

def get_all_collections(current_filter=None) -> Optional[List[Dict]]:
    """Retrieve all collections from Radarr."""
    print(f"🔗 Connecting to Radarr at: {RADARR_HOST}")
    
    collections = make_radarr_request("collection")
    if collections is not None:
        print(f"📚 Found {len(collections)} total collections")
        
        # Use the passed filter or fall back to global
        filter_to_use = current_filter if current_filter is not None else COLLECTION_FILTER
        
        # Apply filtering if specified
        if filter_to_use is not None:
            filtered_collections = []
            for collection in collections:
                title = collection.get('title', '')
                if matches_filter(title, filter_to_use):
                    filtered_collections.append(collection)
            
            filter_display = filter_to_use if isinstance(filter_to_use, str) else ', '.join(filter_to_use)
            print(f"🔍 Filtered to {len(filtered_collections)} collections matching: {filter_display}")
            return filtered_collections
        
        return collections
    else:
        print("❌ Failed to retrieve collections")
        return None

def find_collections_to_update(collections: List[Dict]) -> List[Dict]:
    """Find collections that need path updates."""
    collections_to_update = []
    
    for collection in collections:
        root_path = collection.get('rootFolderPath', '')
        if root_path and root_path.startswith(OLD_PATH_PREFIX):
            old_path = root_path
            new_path = root_path.replace(OLD_PATH_PREFIX, NEW_PATH_PREFIX, 1)
            
            collections_to_update.append({
                'id': collection.get('id'),
                'title': collection.get('title', 'Unknown'),
                'old_path': old_path,
                'new_path': new_path,
                'collection_data': collection
            })
    
    return collections_to_update

def preview_changes(collections_to_update: List[Dict], current_filter=None) -> None:
    """Display a preview of all changes that will be made."""
    filter_to_use = current_filter if current_filter is not None else COLLECTION_FILTER
    
    if filter_to_use:
        filter_display = filter_to_use if isinstance(filter_to_use, str) else ', '.join(filter_to_use)
        print(f"\n📋 Preview of {len(collections_to_update)} collections to update (filtered by: {filter_display}):")
    else:
        print(f"\n📋 Preview of {len(collections_to_update)} collections to update:")
    
    print("=" * 80)
    
    for i, update_info in enumerate(collections_to_update, 1):
        print(f"\n{i}. {update_info['title']} (ID: {update_info['id']})")
        print(f"   Old: {update_info['old_path']}")
        print(f"   New: {update_info['new_path']}")
    
    print("=" * 80)

def confirm_update() -> str:
    """Ask user for confirmation before proceeding."""
    while True:
        response = input("\n❓ Do you want to proceed with these updates? (yes/no/preview/filter): ").lower().strip()
        if response in ['yes', 'y']:
            return 'yes'
        elif response in ['no', 'n']:
            return 'no'
        elif response in ['preview', 'p']:
            return 'preview'
        elif response in ['filter', 'f']:
            return 'filter'
        else:
            print("Please enter 'yes', 'no', 'preview', or 'filter'")

def interactive_filter_setup() -> Optional[str]:
    """Allow user to set up filtering interactively."""
    print("\n🔍 Collection Filter Setup")
    print("=" * 40)
    print("Examples:")
    print("  Marvel*           - Collections starting with 'Marvel'")
    print("  *Marvel*          - Collections containing 'Marvel'")
    print("  Fast & Furious*   - Specific franchise")
    print("  Marvel*,DC*       - Multiple patterns (comma-separated)")
    print("  (empty)           - No filter, process all collections")
    
    filter_input = input("\nEnter filter pattern(s): ").strip()
    
    if not filter_input:
        return None
    
    # Handle comma-separated patterns
    if ',' in filter_input:
        return [pattern.strip() for pattern in filter_input.split(',')]
    
    return filter_input

def update_collection_path(update_info: Dict) -> tuple:
    """Update a single collection's root folder path."""
    collection_data = update_info['collection_data'].copy()
    collection_data['rootFolderPath'] = update_info['new_path']
    
    # Remove read-only fields that might cause issues
    fields_to_remove = ['missingMovies', 'movies']
    for field in fields_to_remove:
        collection_data.pop(field, None)
    
    result = make_radarr_request(
        f"collection/{update_info['id']}", 
        method="PUT", 
        data=collection_data
    )
    
    if result:
        return True, result
    else:
        return False, "API request failed"

def test_connection() -> bool:
    """Test the connection to Radarr API."""
    print("🔍 Testing connection to Radarr...")
    
    result = make_radarr_request("system/status")
    if result:
        print(f"✅ Connected successfully to Radarr v{result.get('version', 'unknown')}")
        return True
    else:
        return False

def main():
    """Main execution function."""
    print("🎬 Radarr Collection Path Update Script (Filtered Version)")
    print("=" * 65)
    
    # Check if API key is set
    if RADARR_API_KEY == "your_api_key_here":
        print("❌ ERROR: Please set your Radarr API key in the script configuration")
        print("   You can find your API key in Radarr -> Settings -> General -> Security")
        print("   Edit this script and replace 'your_api_key_here' with your actual API key")
        sys.exit(1)
    
    # Show current filter settings
    current_filter = COLLECTION_FILTER
    if current_filter:
        filter_display = current_filter if isinstance(current_filter, str) else ', '.join(current_filter)
        print(f"🔍 Collection filter active: {filter_display}")
    else:
        print("📚 No collection filter - will process all collections")
    
    # Test connection
    if not test_connection():
        print("❌ Failed to connect to Radarr. Please check your configuration.")
        sys.exit(1)
    
    # Get all collections (with filtering applied)
    collections = get_all_collections(current_filter)
    if collections is None:
        sys.exit(1)
    
    # Find collections that need updating
    collections_to_update = find_collections_to_update(collections)
    
    if not collections_to_update:
        if current_filter:
            print(f"✅ No collections matching filter '{current_filter}' found with '{OLD_PATH_PREFIX}' prefix.")
        else:
            print(f"✅ No collections found with '{OLD_PATH_PREFIX}' prefix. All collections are already updated!")
        print("   Try adjusting your filter or check if collections have already been updated.")
        return
    
    # Show initial preview
    preview_changes(collections_to_update, current_filter)
    
    # Main interaction loop
    while True:
        confirmation = confirm_update()
        
        if confirmation == 'preview':
            preview_changes(collections_to_update, current_filter)
            continue
        elif confirmation == 'filter':
            new_filter = interactive_filter_setup()
            current_filter = new_filter
            print(f"\n🔄 Reloading collections with new filter...")
            collections = get_all_collections(current_filter)
            if collections is None:
                continue
            collections_to_update = find_collections_to_update(collections)
            if not collections_to_update:
                if current_filter:
                    print(f"✅ No collections matching new filter found with '{OLD_PATH_PREFIX}' prefix.")
                else:
                    print(f"✅ No collections found with '{OLD_PATH_PREFIX}' prefix.")
                continue
            preview_changes(collections_to_update, current_filter)
            continue
        elif confirmation == 'no':
            print("🚫 Update cancelled by user")
            return
        else:  # confirmation == 'yes'
            break
    
    # Perform updates
    print(f"\n🔄 Updating {len(collections_to_update)} collections...")
    
    successful_updates = 0
    failed_updates = 0
    
    for i, update_info in enumerate(collections_to_update, 1):
        print(f"\n[{i}/{len(collections_to_update)}] Updating: {update_info['title']}")
        
        success, result = update_collection_path(update_info)
        
        if success:
            print(f"   ✅ Success: {update_info['old_path']} → {update_info['new_path']}")
            successful_updates += 1
        else:
            print(f"   ❌ Failed: {result}")
            failed_updates += 1
    
    # Summary
    print(f"\n📊 Update Summary:")
    print(f"   ✅ Successful updates: {successful_updates}")
    print(f"   ❌ Failed updates: {failed_updates}")
    print(f"   📚 Total collections processed: {len(collections_to_update)}")
    
    if current_filter:
        filter_display = current_filter if isinstance(current_filter, str) else ', '.join(current_filter)
        print(f"   🔍 Filter used: {filter_display}")
    
    if failed_updates == 0:
        print(f"\n🎉 All filtered collection paths successfully updated from '{OLD_PATH_PREFIX}' to '{NEW_PATH_PREFIX}'!")
    else:
        print(f"\n⚠️  Some updates failed. Please check the errors above and retry if needed.")

if __name__ == "__main__":
    main()
