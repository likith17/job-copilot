"""
Example programmatic usage of JobMatcher as a library.

This shows how to use the modules directly without CLI.
"""

from jobmatcher.parsers.resume_parser import ResumeParser
from jobmatcher.models.company import CompanyConfig
from jobmatcher.crawlers.registry import CrawlerRegistry
from jobmatcher.ranking.engine import RankingEngine
from jobmatcher.filtering.filters import FilterCriteria, filter_jobs
from jobmatcher.exporter.csv_export import export_to_csv, export_to_json


def main():
    # 1. Parse resume
    parser = ResumeParser()
    resume = parser.parse_file("examples/sample_resume.txt")
    print(f"Parsed resume: {resume.name}, {len(resume.skills)} skills")
    print(f"Skills: {sorted(resume.skills)}")

    # 2. Define companies to crawl
    companies = [
        CompanyConfig(name="Airbnb", ats="greenhouse", identifier="airbnb"),
        CompanyConfig(name="Notion", ats="lever", identifier="notion"),
        CompanyConfig(name="Linear", ats="ashby", identifier="linear"),
    ]

    # 3. Crawl (real network requests)
    all_jobs = []
    for company in companies:
        try:
            crawler = CrawlerRegistry.create(company.ats)
            jobs = crawler.fetch_jobs(company)
            print(f"{company.name}: {len(jobs)} jobs")
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"Failed {company.name}: {e}")

    print(f"\nTotal fetched: {len(all_jobs)} jobs")

    # 4. Rank
    engine = RankingEngine()
    ranked = engine.rank_all(resume, all_jobs)
    print(f"\nTop 5 ranked:")
    for r in ranked[:5]:
        print(f"  {r.score:.0f} - {r.job.company} - {r.job.title}")
        print(f"    {r.explanation}")
        print(f"    Matched: {r.matched_skills}")
        print()

    # 5. Filter
    criteria = FilterCriteria(min_score=50, remote_only=False, keywords=["Python", "Backend"])
    filtered = filter_jobs(ranked, criteria)
    print(f"\nFiltered (min 50, python/backend): {len(filtered)} jobs")

    # 6. Export
    export_to_csv(ranked, "ranked_jobs.csv")
    export_to_json(ranked, "ranked_jobs.json")
    print("\nExported to ranked_jobs.csv and ranked_jobs.json")


if __name__ == "__main__":
    main()
