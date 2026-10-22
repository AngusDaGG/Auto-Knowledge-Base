"""Offline fixtures are retained under ignored state/test-runs; no cleanup deletes."""
from pathlib import Path
import hashlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

import kb_knowledge as kb


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).parent / "state" / "test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="knowledge_", dir=fixtures))
        self.folder = self.root / "knowledge" / "douyin"
        self.folder.mkdir(parents=True)
        self.card = self.folder / "12345.md"
        self.index = self.folder.parent / "README.md"
        self.text = '''---
type: distilled_video_knowledge
platform: douyin
video_id: "12345"
title: "部署教學"
author: "作者"
source_url: "https://www.douyin.com/video/12345"
processed_at: "2026-10-22"
topics: [部署, Docker]
evidence_level: "作者建議，未獨立驗證"
---
# 部署教學
## 核心知識
容器部署是方法。
## 方法與步驟
1. 記錄設定。
## 適用條件
有容器環境。
## 限制與風險
未做實機驗證。
## 來源與時間戳
作者於 01:23 提及部署。
'''
        self.card.write_text(self.text, encoding="utf-8")
        self.index.write_text("# 唯一索引\n[卡片](douyin/12345.md)\n", encoding="utf-8")

    def write(self, text):
        self.card.write_text(text, encoding="utf-8")

    def strict_text(self):
        return self.text.replace("topics:", "content_type: tutorial\naliases: [容器, Containers]\ntopics:")

    def test_legacy_and_new_schema_are_separate(self):
        self.assertEqual(len(kb.validate_knowledge(self.root)["cards"]), 1)
        with self.assertRaisesRegex(ValueError, "content_type"):
            kb.validate_knowledge(self.root, ["knowledge/douyin/12345.md"])
        self.write(self.strict_text())
        self.assertIn("1 張採新版", kb.validate_knowledge(self.root, ["knowledge/douyin/12345.md"])["summary"])

    def test_parser_accepts_block_lists_and_rejects_duplicate_keys(self):
        self.write(self.text.replace("[部署, Docker]", "\n  - 部署\n  - Docker"))
        self.assertEqual(kb.parse_card(self.card)["metadata"]["topics"], ["部署", "Docker"])
        self.write(self.text.replace("author:", "title: duplicate\nauthor:"))
        with self.assertRaisesRegex(ValueError, "重複欄位"):
            kb.parse_card(self.card)

    def test_identity_and_platform_mismatch_rejected(self):
        for text in (self.text.replace("/video/12345", "/video/67890"),
                     self.text.replace("www.douyin.com", "youtube.com"),
                     self.text.replace('video_id: "12345"', 'video_id: "67890"')):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(ValueError):
                    kb.validate_knowledge(self.root)

    def test_index_missing_duplicate_and_broken_links_rejected(self):
        for text in ("# 索引\n", "[A](douyin/12345.md) [B](douyin/12345.md)",
                     "[A](douyin/12345.md) [B](missing.md)"):
            self.index.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                kb.validate_knowledge(self.root)

    def test_reference_index_counts_uses_and_rejects_duplicate_card(self):
        self.index.write_text("[卡片][card]\n\n[card]: douyin/12345.md\n", encoding="utf-8")
        self.assertEqual(len(kb.validate_knowledge(self.root)["cards"]), 1)
        self.index.write_text("[卡片][card] [重複][card]\n\n[card]: douyin/12345.md\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重複連結"):
            kb.validate_knowledge(self.root)

    def test_path_escape_and_encoded_escape_rejected(self):
        outside = self.root.parent / (self.root.name + "_outside.md")
        outside.write_text("outside", encoding="utf-8")
        for link in ("../../../" + outside.name, "%2e%2e/%2e%2e/%2e%2e/" + outside.name,
                     "file:///private.txt", "//server/share", "C:\\private.txt"):
            self.write(self.text + f"\n[bad]({link})\n")
            with self.subTest(link=link), self.assertRaises(ValueError):
                kb.validate_knowledge(self.root)

    def test_symlink_component_rejected_without_following(self):
        # Mock only link detection, so this works without Windows symlink privileges.
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == self.folder or original(path)):
            with self.assertRaisesRegex(ValueError, "symlink"):
                kb.validate_knowledge(self.root)

    def test_draft_card_rejected(self):
        self.write(self.text.replace("type:", "status: draft\ntype:", 1))
        with self.assertRaisesRegex(ValueError, "尚未完成"):
            kb.validate_knowledge(self.root)

    def test_new_sections_must_contain_content_and_timestamp(self):
        strict = ["knowledge/douyin/12345.md"]
        for text in (self.strict_text().replace("有容器環境。", ""),
                     self.strict_text().replace("01:23", "unknown"),
                     self.strict_text().replace("aliases: [容器, Containers]", "aliases: []")):
            self.write(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                kb.validate_knowledge(self.root, strict)

    def test_local_transcript_can_be_omitted_but_broken_link_is_rejected(self):
        kb.validate_knowledge(self.root)
        self.write(self.text.replace("topics:", "source_transcript: ../../videos/missing.md\ntopics:"))
        with self.assertRaisesRegex(ValueError, "不存在"):
            kb.validate_knowledge(self.root)
        self.write(self.text.replace("topics:", 'source_transcript: "本機保留，不發布"\ntopics:'))
        kb.validate_knowledge(self.root)

    def test_local_source_is_searchable_but_cannot_pass_publication(self):
        local = self.folder.parent / "local"
        local.mkdir()
        card = local / "local_1234abcd.md"
        card.write_text(self.strict_text().replace("platform: douyin", "platform: local")
                        .replace('video_id: "12345"', 'video_id: "local_1234abcd"')
                        .replace('source_url: "https://www.douyin.com/video/12345"\n', ''), encoding="utf-8")
        self.index.write_text(self.index.read_text(encoding="utf-8") + "[本機](local/local_1234abcd.md)\n", encoding="utf-8")
        self.assertEqual(len(kb.validate_knowledge(self.root)["cards"]), 2)
        with self.assertRaisesRegex(ValueError, "不可發布"):
            kb.validate_knowledge(self.root, ["knowledge/local/local_1234abcd.md"])

    def test_platform_prefixed_id_matches_public_source(self):
        youtube = self.folder.parent / "youtube"
        youtube.mkdir()
        card = youtube / "youtube_Tw18-4U7mts.md"
        card.write_text(self.strict_text().replace("platform: douyin", "platform: youtube")
                        .replace('video_id: "12345"', 'video_id: "youtube_Tw18-4U7mts"')
                        .replace('https://www.douyin.com/video/12345', 'https://www.youtube.com/watch?v=Tw18-4U7mts'), encoding="utf-8")
        self.index.write_text(self.index.read_text(encoding="utf-8") + "[YouTube](youtube/youtube_Tw18-4U7mts.md)\n", encoding="utf-8")
        self.assertEqual(len(kb.validate_knowledge(self.root, ["knowledge/youtube/youtube_Tw18-4U7mts.md"])["cards"]), 2)

    def test_distinct_youtube_watch_sources_are_not_duplicates(self):
        youtube = self.folder.parent / "youtube"
        youtube.mkdir()
        for identity in ("Tw18-4U7mts", "AAAAAAAAAAA"):
            card = youtube / f"youtube_{identity}.md"
            card.write_text(self.text.replace("platform: douyin", "platform: youtube")
                            .replace('video_id: "12345"', f'video_id: "youtube_{identity}"')
                            .replace("https://www.douyin.com/video/12345", f"https://www.youtube.com/watch?v={identity}"), encoding="utf-8")
            self.index.write_text(self.index.read_text(encoding="utf-8") + f"[影片](youtube/youtube_{identity}.md)\n", encoding="utf-8")
        self.assertEqual(len(kb.validate_knowledge(self.root)["cards"]), 3)

    def test_duplicate_short_source_with_tracking_query_is_rejected(self):
        self.write(self.text.replace("https://www.douyin.com/video/12345", "https://v.douyin.com/SHORT/?tracking=first"))
        second = self.folder / "67890.md"
        second.write_text(self.text.replace('video_id: "12345"', 'video_id: "67890"')
                          .replace("https://www.douyin.com/video/12345", "https://v.douyin.com/SHORT/?tracking=second"), encoding="utf-8")
        self.index.write_text(self.index.read_text(encoding="utf-8") + "[重複](douyin/67890.md)\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重複影片來源"):
            kb.validate_knowledge(self.root)

    def test_broken_image_and_reference_links_rejected(self):
        for link in ("![missing](missing.png)", "[source][link]\n[link]: missing.md"):
            self.write(self.text + "\n" + link)
            with self.subTest(link=link), self.assertRaises(ValueError):
                kb.validate_knowledge(self.root)

    def test_strict_card_cannot_link_unpublished_local_transcript(self):
        source = self.root / "videos" / "douyin" / "12345.md"
        source.parent.mkdir(parents=True)
        source.write_text("local transcript", encoding="utf-8")
        self.write(self.strict_text() + "\n[逐字稿](../../videos/douyin/12345.md)\n")
        kb.validate_knowledge(self.root)
        with self.assertRaisesRegex(ValueError, "knowledge 外"):
            kb.validate_knowledge(self.root, ["knowledge/douyin/12345.md"])

    @unittest.skipUnless(shutil.which("rg"), "requires installed rg")
    def test_real_rg_alias_search_and_no_results(self):
        self.write(self.strict_text())
        for query in ("容器", "containers"):
            result = kb.search_knowledge(self.root, query, limit=1)
            self.assertFalse(result["no_results"])
            self.assertEqual(len(result["matches"]), 1)
            self.assertEqual(result["index"], "knowledge/README.md")
            self.assertEqual(result["matches"][0]["path"], "knowledge/douyin/12345.md")
        self.assertTrue(kb.search_knowledge(self.root, "不存在的字串")["no_results"])

    @unittest.skipUnless(shutil.which("rg"), "requires installed rg")
    def test_search_option_and_shell_metacharacters_stay_literal(self):
        for query in ("--help", "$(echo secret)", "; echo test", "a.*b"):
            self.assertTrue(kb.search_knowledge(self.root, query)["no_results"])

    def test_search_validates_all_cards_before_running(self):
        self.write(self.text.replace("type: distilled_video_knowledge", "type: draft"))
        with patch.object(kb.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                kb.search_knowledge(self.root, "anything")
            run.assert_not_called()

    def test_search_input_limits(self):
        for query, limit in (("", 20), ("a\nb", 20), ("a", 0), ("a", 201), ("a", True)):
            with self.subTest(query=query, limit=limit), self.assertRaises(ValueError):
                kb.search_knowledge(self.root, query, limit)


class NamedDocumentTests(unittest.TestCase):
    setUp = KnowledgeTests.setUp
    strict_text = KnowledgeTests.strict_text
    def document_text(self, title="研究方法", digest=None):
        digest = digest or hashlib.sha256(b"local original research").hexdigest()
        return f'''---
type: distilled_document_knowledge
platform: document
document_id: "doc_{digest}"
source_sha256: "{digest}"
source_document: "local:research.md"
title: "{title}"
author: "原文作者未明"
processed_at: "2026-10-22"
topics: [研究, 方法]
aliases: [研究流程, research workflow]
content_type: user_imported_markdown
evidence_level: "來源原文主張；未外部驗證"
status: completed
---
# {title}
## 核心知識
將觀點與證據分開記錄。
## 方法
先記錄觀點，再檢查是否有支持來源。
## 適用條件
適用於人工文件整理。
## 限制
這份卡片沒有獨立驗證原文結論，也不是全文或工程驗收報告。
## 來源依據
原始檔名 research.md；SHA256：{digest}；原文章節「研究方法」。
'''

    def add_document(self, title="研究方法", digest=None, body=None):
        text = body if body is not None else self.document_text(title, digest)
        digest = digest or hashlib.sha256(b"local original research").hexdigest()
        folder = self.root / "knowledge" / "cards"
        folder.mkdir(exist_ok=True)
        path = folder / kb.card_filename(title, "document", "doc_" + digest)
        path.write_text(text, encoding="utf-8")
        self.index.write_text(self.index.read_text(encoding="utf-8") + f"[文件](<cards/{path.name}>)\n", encoding="utf-8")
        return path

    def test_document_requires_no_video_url_or_timestamp(self):
        path = self.add_document("研究 methods")
        card = kb.parse_card(path)
        self.assertNotIn("video_id", card["metadata"])
        self.assertNotIn("source_url", card["metadata"])
        self.assertEqual(len(kb.validate_knowledge(self.root)["cards"]), 2)
        text = path.read_text(encoding="utf-8").replace("user_imported_markdown", "user_import")
        path.write_text(text, encoding="utf-8")
        kb.validate_knowledge(self.root, [path.relative_to(self.root).as_posix()])

    def test_document_source_hash_and_original_section_evidence_are_required(self):
        path = self.add_document()
        valid = path.read_text(encoding="utf-8")
        for invalid in (valid.replace('source_sha256: "', 'missing_source_sha256: "'),
                        valid.replace('source_document: "local:research.md"', 'source_document: "local:C:/private/research.md"'),
                        valid.replace("；原文章節「研究方法」。", "。")):
            path.write_text(invalid, encoding="utf-8")
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                kb.validate_knowledge(self.root)

    def test_document_identity_cannot_use_an_unrelated_source_hash(self):
        path = self.add_document()
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(b"local original research").hexdigest()
        path.write_text(text.replace('source_sha256: "' + digest, 'source_sha256: "' + "0" * 64), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "document_id.*source_sha256"):
            kb.validate_knowledge(self.root)

    def test_same_document_with_a_different_title_is_still_a_duplicate(self):
        self.add_document("研究方法")
        self.add_document("重新命名後的研究")
        with self.assertRaisesRegex(ValueError, "重複影片來源"):
            kb.validate_knowledge(self.root)

    def test_named_video_and_legacy_card_deduplicate_by_metadata(self):
        folder = self.root / "knowledge" / "cards"
        folder.mkdir()
        title = "部署教學"
        path = folder / kb.card_filename(title, "douyin", "12345")
        path.write_text(self.strict_text(), encoding="utf-8")
        self.index.write_text(self.index.read_text(encoding="utf-8") + f"[新版](cards/{path.name})\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重複影片來源"):
            kb.validate_knowledge(self.root)

    def test_new_cards_reject_raw_and_engineering_report_content_types(self):
        path = self.add_document()
        valid = path.read_text(encoding="utf-8")
        for invalid in ("# Original full document\nUnrefined source.\n", "# Engineering report\nPASS\n",
                        valid.replace("content_type: user_imported_markdown", "content_type: engineering_report"),
                        valid.replace("aliases: [研究流程, research workflow]", "aliases: []")):
            path.write_text(invalid, encoding="utf-8")
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                kb.validate_knowledge(self.root)


class FilenameTests(unittest.TestCase):
    def test_windows_name_sanitization_and_id_stability(self):
        value = kb.card_filename('CON. ', "youtube", "youtube_Tw18-4U7mts")
        self.assertEqual(value, "知識_CON__youtube__Tw18-4U7mts.md")
        value = kb.card_filename('A:/B\\C?*<>|" title. ', "douyin", "12345")
        self.assertFalse(any(char in value for char in '<>:"/\\|?*'))
        self.assertTrue(value.endswith("__douyin__12345.md"))
        self.assertTrue(kb.is_card_filename(value))

    def test_filename_length_unicode_and_reserved_names(self):
        for title in ("😀" * 200, "a" * 500, "NUL.txt", "COM9", "...", "A__B"):
            value = kb.card_filename(title, "document", "doc_" + "a" * 64)
            self.assertLessEqual(len(value.encode("utf-16-le")) // 2, 150)
            self.assertTrue(kb.is_card_filename(value))
            self.assertTrue(value.endswith("__document__doc_" + "a" * 64 + ".md"))

    def test_source_id_cannot_escape_and_valid_underscores_are_preserved(self):
        for identity in ("../secret", "a/b", "C:secret", "", "a" * 97):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                kb.card_filename("title", "youtube", identity)
        name = kb.card_filename("title", "youtube", "ab__cdefghi")
        self.assertTrue(kb.is_card_filename(name))
        for name in ("README.md", "AGENTS.md", "report.md", "title__unknown__id.md", "CON__youtube__id.md"):
            self.assertFalse(kb.is_card_filename(name))

    def test_windows_superscript_device_names_are_not_used_before_extension(self):
        for title in ("COM¹.txt", "LPT².anything", "NUL .txt"):
            with self.subTest(title=title):
                self.assertTrue(kb.card_filename(title, "youtube", "abcdefghijk").startswith("知識_"))


if __name__ == "__main__":
    unittest.main()
