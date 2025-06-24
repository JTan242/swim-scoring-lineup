from flask_wtf import FlaskForm
from wtforms import (
    StringField, PasswordField, SelectField,
    IntegerField, BooleanField, widgets, SelectMultipleField
)
from wtforms.validators import DataRequired, NumberRange

class MultiCheckboxField(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])

class RegistrationForm(LoginForm):
    pass

class ScrapeForm(FlaskForm):
    team_name = StringField('Team Name', validators=[DataRequired()])
    team_id   = IntegerField('Team ID', validators=[DataRequired()])
    gender    = SelectField(
        'Gender', choices=[('M','Male'),('F','Female')],
        validators=[DataRequired()]
    )
    year      = IntegerField(
        'Year', validators=[DataRequired(), NumberRange(min=1900, max=2100)]
    )
    pro       = BooleanField('Pro Team? (college if unchecked)')

class SelectionForm(FlaskForm):
    teams = MultiCheckboxField('Team & Season', coerce=str)
    event = SelectField('Event', coerce=str, validators=[DataRequired()])
    top_n = IntegerField(
        'Number of places', default=8,
        validators=[DataRequired(), NumberRange(min=1, max=16)]
    )
    scoring_mode = SelectField(
        'Relay Scoring Mode',
        choices=[('unscored','Non-Scoring'),('scored','Scoring')],
        default='unscored'
    )