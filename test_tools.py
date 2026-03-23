import asyncio
import os
import json
from server import get_xcos_block_data, get_xcos_block_source, search_related_xcos_files

async def test_tools():
    print("Testing get_xcos_block_data...")
    # Mock files for test
    os.makedirs("./data/reference_blocks", exist_ok=True)
    os.makedirs("./data/blocks", exist_ok=True)
    os.makedirs("./data/help", exist_ok=True)

    with open("./data/reference_blocks/TEST_BLOCK.xcos", 'w') as f: f.write("<xml>example</xml>")
    with open("./data/blocks/TEST_BLOCK.json", 'w') as f: f.write('{"info": "test"}')
    with open("./data/help/TEST_BLOCK.xml", 'w', encoding='utf-8') as f:
        f.write('<refentry xmlns="http://docbook.org/ns/docbook" xml:id="TEST_BLOCK">'
                '<refsection id="Dialogbox_TEST_BLOCK"><title>Params</title><para>Content</para></refsection>'
                '</refentry>')

    res = await get_xcos_block_data("TEST_BLOCK")
    print(res[0].text)

    print("\nTesting get_xcos_block_source...")
    os.makedirs("./data/macros/sub", exist_ok=True)
    with open("./data/macros/sub/TEST_BLOCK.sci", 'w') as f: f.write("function TEST_BLOCK()")
    res = await get_xcos_block_source("TEST_BLOCK")
    print(res[0].text)

    print("\nTesting search_related_xcos_files...")
    res = await search_related_xcos_files("TEST_BLOCK")
    print(res[0].text)

if __name__ == "__main__":
    asyncio.run(test_tools())
