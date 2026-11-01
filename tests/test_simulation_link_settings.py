from bb.settings import Settings


def test_training_destinations_default_to_empty(tmp_path):
    settings = Settings(env={
        "BB_HOME": str(tmp_path),
        "BB_BASE_URL": "https://bb.example.com",
    })
    assert settings.training_destination_hostnames == ()


def test_training_destinations_from_operator_environment(tmp_path):
    settings = Settings(env={
        "BB_HOME": str(tmp_path),
        "BB_BASE_URL": "https://bb.example.com",
        "BB_TRAINING_DESTINATIONS": (
            "Training.Example.com, learn.example.org, training.example.com"
        ),
    })
    assert settings.training_destination_hostnames == (
        "learn.example.org",
        "training.example.com",
    )


def test_empty_entries_are_ignored(tmp_path):
    settings = Settings(env={
        "BB_HOME": str(tmp_path),
        "BB_TRAINING_DESTINATIONS": " , , ",
    })
    assert settings.training_destination_hostnames == ()
