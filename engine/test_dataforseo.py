import os
import sys

# Setup Django environment so we can run this script standalone
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from apps.connectors.dataforseo.client import DataForSEOClient

def run_test():
    # We will use your free trial credentials here
    print("--------------------------------------------------")
    print("Testing DataForSEO Python Connector")
    print("--------------------------------------------------\n")
    
    # IMPORTANT: Replace these with your actual DataForSEO login and password before running!
    API_LOGIN = "haiqashujayat111@gmail.com" 
    API_PASSWORD = "1b23b4c5c1e10317"

    client = DataForSEOClient(login=API_LOGIN, password=API_PASSWORD)

    print("1. Testing Keyword Ideas...")
    hardcoded_keywords = ["running shoes", "gym clothes"]
    print(f"Sending keywords: {hardcoded_keywords}")
    ideas = client.get_keyword_ideas(hardcoded_keywords, limit=2)
    for idea in ideas:
        print(f"  -> Found: '{idea.keyword}' (Volume: {idea.search_volume})")
        
    print("\n2. Testing Competitor Discovery...")
    domains = client.get_competitor_domains("nike.com", limit=2)
    for d in domains:
        print(f"  -> Competitor found: {d.domain}")

    print("\n✅ All Python Connector tests passed successfully!")

if __name__ == "__main__":
    run_test()
