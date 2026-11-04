from app.services.plaza.public_catalog.package_parser import normalize_skill_name


def test_skill_name_policy_treats_case_spaces_underscores_and_dashes_as_equal() -> None:
    assert normalize_skill_name("  Report_Writer ") == "report writer"
    assert normalize_skill_name("report-writer") == "report writer"
    assert normalize_skill_name("REPORT WRITER") == "report writer"
