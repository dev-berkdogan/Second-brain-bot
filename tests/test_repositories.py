import os
import unittest
import tempfile
import shutil


class TestRepositories(unittest.TestCase):

    def setUp(self):
        # Her test için ayrı geçici SQLite database
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        os.environ["DB_PATH"] = self.db_path

        # Migration'ı test database'i üzerinde çalıştır
        from scripts.migrate_sqlite_v1 import run_migration
        run_migration(target_db_path=self.db_path)

        # Repository'leri import et
        from app.db.repositories.users import UserRepository
        from app.db.repositories.channels import ChannelRepository
        from app.db.repositories.contents import ContentRepository
        from app.db.repositories.user_contents import UserContentRepository

        self.user_repo = UserRepository
        self.channel_repo = ChannelRepository
        self.content_repo = ContentRepository
        self.user_content_repo = UserContentRepository

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_idempotent_migration(self):
        """
        Migration ikinci kez çalıştırıldığında
        mevcut veriyi değiştirmemeli.
        """
        from scripts.migrate_sqlite_v1 import run_migration

        run_migration(target_db_path=self.db_path)

        self.assertTrue(os.path.exists(self.db_path))

    def test_user_and_channel_creation(self):
        """
        User + channel oluşturma ve retrieval testi.
        """
        user_id = "user_456"

        self.user_repo.upsert_user(
            user_id,
            preferred_language="English"
        )

        user = self.user_repo.get_user(user_id)

        self.assertIsNotNone(user)
        self.assertEqual(
            user["preferred_language"],
            "English"
        )

        self.channel_repo.upsert_channel(
            user_id=user_id,
            channel_type="telegram",
            external_user_id="456",
            display_name="Test User",
            username="testuser"
        )

        channel = self.channel_repo.get_channel_by_external_id(
            "telegram",
            "456"
        )

        self.assertIsNotNone(channel)
        self.assertEqual(
            channel["username"],
            "testuser"
        )

    def test_content_lifecycle_and_translation(self):
        """
        Content oluşturma, status değiştirme,
        analysis + translation ve user save testi.
        """

        user_id = "user_456"

        self.user_repo.upsert_user(
            user_id,
            preferred_language="English"
        )

        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

        content_id = self.content_repo.create_content(
            canonical_url=url,
            source_type="video"
        )

        content = self.content_repo.get_by_id(content_id)

        self.assertIsNotNone(content)

        self.assertEqual(
            content["source_platform"],
            "youtube"
        )

        self.assertEqual(
            content["source_type"],
            "video"
        )

        self.assertEqual(
            content["status"],
            "received"
        )

        self.assertIsNone(
            content["processed_at"]
        )

        # Processing tamamlandı
        self.content_repo.update_status(
            content_id,
            "completed"
        )

        updated_content = self.content_repo.get_by_id(
            content_id
        )

        self.assertIsNotNone(
            updated_content["processed_at"]
        )

        # Analysis
        self.content_repo.save_canonical_analysis(
            content_id=content_id,
            summary="Canonical Summary",
            raw_analysis="Raw analysis",
            language="English"
        )

        # Translation
        self.content_repo.save_translation(
            content_id,
            "Türkçe",
            "Çevrilmiş Türkçe Özet"
        )

        translation = self.content_repo.get_translation(
            content_id,
            "Türkçe"
        )

        self.assertEqual(
            translation,
            "Çevrilmiş Türkçe Özet"
        )

        # User -> Content relation
        user_content_id = self.user_content_repo.save_content(
            user_id=user_id,
            content_id=content_id,
            user_language="English"
        )

        self.assertIsNotNone(
            user_content_id
        )

        self.assertTrue(
            self.user_content_repo.is_saved(
                user_id,
                content_id
            )
        )


if __name__ == "__main__":
    unittest.main()