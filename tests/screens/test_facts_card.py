

def test_a_compare_column_takes_its_facts_under_other_names_and_half_a_comparison_is_no_card():
    import asyncio

    from littlebot.screens.cards import facts

    named = {"card": "facts", "layout": "compare", "title": "Genghis Khan vs Alexander the Great",
             "columns": [{"name": "Genghis Khan", "facts": ["1162-1227", "Mongol Empire"]},
                         {"name": "Alexander the Great", "points": ["356-323 BC", "Macedon"]}]}
    card = facts.FactsCard(named, None)
    assert [c["facts"] for c in card.columns] == [["1162-1227", "Mongol Empire"], ["356-323 BC", "Macedon"]]
    half = {**named, "columns": [named["columns"][0], {"name": "Alexander the Great", "facts": []}]}
    assert facts.half_compared(half) and not facts.half_compared(named)
    assert asyncio.run(facts._build(half, None, None)) is None
    three = {**named, "columns": [*named["columns"], {"name": "Caesar"}]}
    assert [c["name"] for c in facts.FactsCard(three, None).columns] == ["Genghis Khan", "Alexander the Great"]
