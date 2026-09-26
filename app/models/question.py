from app.extensions import db


class Question(db.Model):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(128), unique=True, nullable=True, index=True)
    statement_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    level = db.Column(db.Integer, nullable=False, index=True)  # 1-5, matches tier.level
    statement = db.Column(db.Text, nullable=False)
    statement_latex = db.Column(db.Text, nullable=True)
    answer = db.Column(db.String(512), nullable=False)
    answer_type = db.Column(db.String(16), nullable=False)  # 'mcq' | 'numeric' | 'exact'
    choices_json = db.Column(db.Text, nullable=True)  # JSON list, only for 'mcq'
    topic = db.Column(db.String(128), nullable=True, index=True)
    source = db.Column(db.String(128), nullable=True)
    times_served = db.Column(db.Integer, default=0, nullable=False)
    times_correct = db.Column(db.Integer, default=0, nullable=False)

    def to_client_dict(self):
        """Never includes the answer -- server is the only source of truth."""
        import json

        return {
            "id": self.id,
            "statement": self.statement,
            "statement_latex": self.statement_latex,
            "answer_type": self.answer_type,
            "choices": json.loads(self.choices_json) if self.choices_json else None,
            "topic": self.topic,
        }
