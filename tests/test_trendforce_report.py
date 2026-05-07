from src.trendforce_report import parse_price_page, add_comparisons, add_spreads


def test_parse_compare_and_spread():
    html = """
    <html><body><h2>DRAM 價格趨勢</h2>
    <h3>DRAM Spot Price (未稅)</h3><p>Last Update 2026-05-07 14:40 (GMT+8)</p>
    <table><tr><th>項目</th><th>盤高點</th><th>盤低點</th><th>盤平均</th><th>盤漲跌幅</th></tr>
    <tr><td>DDR5 16Gb</td><td>51.00</td><td>28.00</td><td>39.00</td><td>▲ 0.43 %</td></tr></table>
    <h3>DRAM Contract Price (未稅)</h3><p>Last Update 2026-05-07</p>
    <table><tr><th>項目</th><th>高點</th><th>低點</th><th>均價</th><th>均價漲跌</th></tr>
    <tr><td>DDR5 16Gb</td><td>52.00</td><td>29.00</td><td>40.00</td><td>▲ 1.00 %</td></tr></table>
    </body></html>
    """
    quotes = parse_price_page(html, "https://example.test/price/dram/dram_spot")
    assert len(quotes) == 2
    assert quotes[0].product_name == "DDR5 16Gb"
    assert quotes[0].quote_value == 39.0
    add_comparisons(quotes, {quotes[0].stable_key: {"quote_value": 38.0}})
    add_spreads(quotes)
    assert round(quotes[0].yesterday_change_percent, 2) == 2.63
    assert quotes[1].spread_value == 1.0
    assert quotes[1].spread_label == "正價差"
