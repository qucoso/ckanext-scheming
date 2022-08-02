import json
import datetime
from collections import defaultdict
import itertools
import logging

import pytz
import six
import string
import requests

import ckan.lib.helpers as h
from ckan.lib.navl.dictization_functions import convert
from ckantoolkit import (
    get_validator,
    UnknownValidator,
    missing,
    Invalid,
    StopOnError,
    _
)
from decimal import Decimal, DecimalException

import ckanext.scheming.helpers as sh
from ckanext.scheming.errors import SchemingException

OneOf = get_validator('OneOf')
ignore_missing = get_validator('ignore_missing')
not_empty = get_validator('not_empty')

all_validators = {}

def register_validator(fn):
    """
    collect validator functions into ckanext.scheming.all_helpers dict
    """
    all_validators[fn.__name__] = fn
    return fn


def scheming_validator(fn):
    """
    Decorate a validator that needs to have the scheming fields
    passed with this function. When generating navl validator lists
    the function decorated will be called passing the field
    and complete schema to produce the actual validator for each field.
    """
    fn.is_a_scheming_validator = True
    return fn


@scheming_validator
@register_validator
def scheming_subfields(field, schema):
    """
    A special validator used to collect and pack subfields.
    """
    from ckanext.scheming.plugins import _field_create_validators

    def subfields_validator(key, data, errors, context):
        # If the field is coming from the API the value will be set directly.
        value = data.get(key)
        if not value:
            # ... otherwise, it's a form submission so our values are stuck
            # unrolled in __extras.
            # If we're working on a package field, the key will look like:
            #   (<field name>,)
            # and if we're working on a resource it'll be:
            #   ('resources', <resource #>, <field name>)
            _junk = data.get(key[:-1] + ('__junk',), {})

            # Group our unrolled fields by their index.
            values = defaultdict(dict)
            for k in _junk.keys():
                if k[0] == key[0]:
                    name = k[2]
                    index = k[1]
                    # Always pop, we don't want handled values to remain in
                    # __extras or they'll end up on the model.
                    values[index][name] = _junk.pop(k)

            # ... then turn it back into an ordered list.
            value = [v for k, v in sorted(values.items())]
        elif isinstance(value, six.string_types):
            value = json.loads(value)

        if not isinstance(value, list):
            # We treat all subfields as repeatable when processing, even
            # when they aren't defined that way in the schema.
            value = [value]

        for subfield in field.get('repeating_subfields', field.get('simple_subfields')):
            validators = _field_create_validators(subfield, schema, False)
            for entry in value:
                # This right here is why we recommend globally unique field
                # names, else you risk trampling values from the top-level
                # schema. Some validators like require_when_published require
                # other top-level fields.
                entry_as_data = {(k,): v for k, v in entry.items()}
                entry_as_data.update(data)

                entry_errors = defaultdict(list)

                for v in validators:
                    convert(
                        v,
                        (subfield['field_name'],),
                        entry_as_data,
                        entry_errors,
                        context
                    )

                # Any subfield errors should be added as errors to the parent
                # since this is the only way we have to let other plugins know
                # of issues.
                errors[key].extend(
                    itertools.chain.from_iterable(
                        v for v in entry_errors.itervalues()
                    )
                )

                # Pull our potentially modified fields back. What if validators
                # modified other fields such as a top-level field? Is this
                # "allowed" in CKAN validators? We might have to replace
                # entry_as_data with a write-tracing dict to capture all
                # changes.
                for k in entry.keys():
                    entry[k] = entry_as_data[(k,)]

        # It would be preferable to just always store as a list, but some plugins
        # such as ckanext-restricted make assumptions on how values are stored.

        if 'repeating_subfields' in field:
            data[key] = json.dumps(value)
        elif value:
            data[key] = json.dumps(value[0])

    return subfields_validator


@scheming_validator
@register_validator
def scheming_choices(field, schema):
    """
    Require that one of the field choices values is passed.
    """
    if 'choices' in field:
        return OneOf([c['value'] for c in field['choices']])

    def validator(value):
        if value is missing or not value:
            return value
        choices = sh.scheming_field_choices(field)
        for choice in choices:
            if value == choice['value']:
                return value
        raise Invalid(_('unexpected choice "%s"') % value)

    return validator


@scheming_validator
@register_validator
def scheming_required(field, schema):
    """
    not_empty if field['required'] else ignore_missing
    """
    if field.get('required'):
        return not_empty
    return ignore_missing


@scheming_validator
@register_validator
def scheming_multiple_choice(field, schema):
    """
    Accept zero or more values from a list of choices and convert
    to a json list for storage:

    1. a list of strings, eg.:

       ["choice-a", "choice-b"]

    2. a single string for single item selection in form submissions:

       "choice-a"
    """
    static_choice_values = None
    if 'choices' in field:
        static_choice_order = [c['value'] for c in field['choices']]
        static_choice_values = set(static_choice_order)

    def validator(key, data, errors, context):
        # if there was an error before calling our validator
        # don't bother with our validation
        if errors[key]:
            return

        value = data[key]
        if value is not missing:
            if isinstance(value, six.string_types):
                value = [value]
            elif not isinstance(value, list):
                errors[key].append(_('expecting list of strings'))
                return
        else:
            value = []

        choice_values = static_choice_values
        if not choice_values:
            choice_order = [
                choice['value']
                for choice in sh.scheming_field_choices(field)
            ]
            choice_values = set(choice_order)

        selected = set()
        for element in value:
            if element in choice_values:
                selected.add(element)
                continue
            errors[key].append(_('unexpected choice "%s"') % element)

        if not errors[key]:
            data[key] = json.dumps([
                v for v in
                (static_choice_order if static_choice_values else choice_order)
                if v in selected
            ])

            if field.get('required') and not selected:
                errors[key].append(_('Select at least one'))

    return validator


def validate_date_inputs(field, key, data, extras, errors, context):
    date_error = _('Date format incorrect')
    time_error = _('Time format incorrect')

    date = None

    def get_input(suffix):
        inpt = key[0] + '_' + suffix
        new_key = (inpt,) + tuple(x for x in key if x != key[0])
        key_value = extras.get(inpt)
        data[new_key] = key_value
        errors[new_key] = []

        if key_value:
            del extras[inpt]

        if field.get('required'):
            not_empty(new_key, data, errors, context)

        return new_key, key_value

    date_key, value = get_input('date')
    value_full = ''

    if value:
        try:
            value_full = value
            date = h.date_str_to_datetime(value)
        except (TypeError, ValueError) as e:
            errors[date_key].append(date_error)

    time_key, value = get_input('time')
    if value:
        if not value_full:
            errors[date_key].append(
                _('Date is required when a time is provided'))
        else:
            try:
                value_full += ' ' + value
                date = h.date_str_to_datetime(value_full)
            except (TypeError, ValueError) as e:
                errors[time_key].append(time_error)

    tz_key, value = get_input('tz')
    if value:
        if value not in pytz.all_timezones:
            errors[tz_key].append('Invalid timezone')
        else:
            if isinstance(date, datetime.datetime):
                date = pytz.timezone(value).localize(date)

    return date


@scheming_validator
@register_validator
def scheming_isodatetime(field, schema):
    def validator(key, data, errors, context):
        value = data[key]
        date = None

        if value:
            if isinstance(value, datetime.datetime):
                return value
            else:
                try:
                    date = h.date_str_to_datetime(value)
                except (TypeError, ValueError) as e:
                    raise Invalid(_('Date format incorrect'))
        else:
            extras = data.get(('__extras',))
            if not extras or (key[0] + '_date' not in extras and
                              key[0] + '_time' not in extras):
                if field.get('required'):
                    not_empty(key, data, errors, context)
            else:
                date = validate_date_inputs(
                    field, key, data, extras, errors, context)

        data[key] = date

    return validator


@scheming_validator
@register_validator
def scheming_isodatetime_tz(field, schema):
    def validator(key, data, errors, context):
        value = data[key]
        date = None

        if value:
            if isinstance(value, datetime.datetime):
                date = sh.scheming_datetime_to_utc(value)
            else:
                try:
                    date = sh.date_tz_str_to_datetime(value)
                except (TypeError, ValueError) as e:
                    raise Invalid(_('Date format incorrect'))
        else:
            extras = data.get(('__extras',))
            if not extras or (key[0] + '_date' not in extras and
                              key[0] + '_time' not in extras):
                if field.get('required'):
                    not_empty(key, data, errors, context)
            else:
                date = validate_date_inputs(
                    field, key, data, extras, errors, context)
                if isinstance(date, datetime.datetime):
                    date = sh.scheming_datetime_to_utc(date)

        data[key] = date

    return validator


@register_validator
def scheming_valid_json_object(value, context):
    """Store a JSON object as a serialized JSON string

    It accepts two types of inputs:
        1. A valid serialized JSON string (it must be an object or a list)
        2. An object that can be serialized to JSON

    """
    if not value:
        return
    elif isinstance(value, six.string_types):
        try:
            loaded = json.loads(value)

            if not isinstance(loaded, dict):
                raise Invalid(
                    _('Unsupported value for JSON field: {}').format(value)
                )

            return value
        except (ValueError, TypeError) as e:
            raise Invalid(_('Invalid JSON string: {}').format(e))

    elif isinstance(value, dict):
        try:
            return json.dumps(value)
        except (ValueError, TypeError) as e:
            raise Invalid(_('Invalid JSON object: {}').format(e))
    else:
        raise Invalid(
            _('Unsupported type for JSON field: {}').format(type(value))
        )


@register_validator
def scheming_load_json(value, context):
    if isinstance(value, six.string_types):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


@register_validator
def scheming_multiple_choice_output(value):
    """
    return stored json as a proper list
    """
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return [value]


def validators_from_string(s, field, schema):
    """
    convert a schema validators string to a list of validators

    e.g. "if_empty_same_as(name) unicode" becomes:
    [if_empty_same_as("name"), unicode]
    """
    out = []
    parts = s.split()
    for p in parts:
        if '(' in p and p[-1] == ')':
            name, args = p.split('(', 1)
            args = args[:-1].split(',')  # trim trailing ')', break up
            v = get_validator_or_converter(name)(*args)
        else:
            v = get_validator_or_converter(p)
        if getattr(v, 'is_a_scheming_validator', False):
            v = v(field, schema)
        out.append(v)
    return out


def get_validator_or_converter(name):
    """
    Get a validator or converter by name
    """
    if name == 'unicode':
        return six.text_type
    try:
        v = get_validator(name)
        return v
    except UnknownValidator:
        pass
    raise SchemingException('validator/converter not found: %r' % name)


def convert_from_extras_group(key, data, errors, context):
    """Converts values from extras, tailored for groups."""

    def remove_from_extras(data, key):
        to_remove = []
        for data_key, data_value in data.items():
            if (data_key[0] == 'extras'
                    and data_key[1] == key):
                to_remove.append(data_key)
        for item in to_remove:
            del data[item]

    for data_key, data_value in data.items():
        if (data_key[0] == 'extras'
            and 'key' in data_value
                and data_value['key'] == key[-1]):
            data[key] = data_value['value']
            break
    else:
        return
    remove_from_extras(data, data_key[1])


@register_validator
def convert_to_json_if_date(date, context):
    if isinstance(date, datetime.datetime):
        return date.date().isoformat()
    elif isinstance(date, datetime.date):
        return date.isoformat()
    else:
        return date


@register_validator
def convert_to_json_if_datetime(date, context):
    if isinstance(date, datetime.datetime):
        return date.isoformat()

    return date


@scheming_validator
@register_validator
def scheming_multiple_text(field, schema):
    """
    Accept repeating text input in the following forms and convert to a json list
    for storage. Also act like scheming_required to check for at least one non-empty
    string when required is true:

    1. a list of strings, eg.

       ["Person One", "Person Two"]

    2. a single string value to allow single text fields to be
       migrated to repeating text

       "Person One"
    """
    def _scheming_multiple_text(key, data, errors, context):
        # just in case there was an error before our validator,
        # bail out here because our errors won't be useful
        if errors[key]:
            return

        value = data[key]
        # 1. list of strings or 2. single string
        if value is not missing:
            if isinstance(value, six.string_types):
                value = [value]
            if not isinstance(value, list):
                errors[key].append(_('expecting list of strings'))
                raise StopOnError

            out = []
            for element in value:
                if not element:
                    continue

                if not isinstance(element, six.string_types):
                    errors[key].append(_('invalid type for repeating text: %r')
                                       % element)
                    continue
                if isinstance(element, six.binary_type):
                    try:
                        element = element.decode('utf-8')
                    except UnicodeDecodeError:
                        errors[key]. append(_('invalid encoding for "%s" value')
                                            % element)
                        continue

                out.append(element)

            if errors[key]:
                raise StopOnError

            data[key] = json.dumps(out)

        if (data[key] is missing or data[key] == '[]') and field.get('required'):
            errors[key].append(_('Missing value'))
            raise StopOnError

    return _scheming_multiple_text


@register_validator
def repeating_text_output(value):
    """
    Return stored json representation as a list, if
    value is already a list just pass it through.
    """
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        return json.loads(value)
    except ValueError:
        return [value]

    
"""
Additional validators
GeoinformationSystems @ TUD
"""

@scheming_validator
@register_validator
def scheming_choices_ignore_missing(field, schema):
    """
    Require that one of the field choices values is passed.
    
    Edit from original: minor fix for ignoring empty value 
    """
    def validator(value):
        if value is missing or not value:
            return value
        choices = sh.scheming_field_choices(field)
        for choice in choices:
            if value == choice['value']:
                return value
        raise Invalid(_('unexpected choice "%s"') % value)

    return validator

@scheming_validator
@register_validator
def check_required_field(field, schema):
    """
    Require value if other field is defined.
    Define 'required_if_value_in' as field parameter in schema.
    """
    def validator(key, data, errors, context):
        value = data.get(key)
        try:
            other_value = data[(field.get('required_if_value_in'),)]
        except:
            other_value = ""
            
        if (not value or value is missing) :
            if (not other_value or other_value is missing):
                return value
            raise Invalid(_('Required since "%s" is defined.') % sh.scheming_field_by_name(schema['dataset_fields'], field.get('required_if_value_in'))['label'])
        return value
        
    return validator
    
@register_validator
def if_not_missing_package_id_or_name_exists(value, context):
    if value:

        model = context['model']
        session = context['session']

        result = session.query(model.Package).get(value)
        if result:
            return value

        result = session.query(model.Package).filter_by(name=value).first()
        if not result:
            raise Invalid('%s: %s' % (_('Dataset not found'), value))

    return value

@register_validator
def if_not_missing_package_id_or_name_exists_list(key, data, errors, context):
    '''Takes a list of package identifiers that is a comma-separated string and validates each.'''
    if isinstance(data[key], six.string_types):
        datasets = [ds.strip()
                    for ds in data[key].split(',')
                    if ds.strip()]
    else:
        datasets = data[key]

    for ds in datasets:
        if_not_missing_package_id_or_name_exists(ds, context)

@register_validator
def single_link_validator(value, context):
    ''' Checks that the provided value (if it is present) is a valid URL '''

    if value:
        pieces = six.moves.urllib.parse.urlparse(value)
        if all([pieces.scheme, pieces.netloc]) and \
           set(pieces.netloc) <= set(string.ascii_letters + string.digits + '-.') and \
           pieces.scheme in ['http', 'https']:
            return value
        else:
            raise Invalid('Please provide a valid link: %s' % value)
    return

@register_validator
def link_list_string_convert(key, data, errors, context):
    '''Takes a list of links that is a comma-separated string and validates each.'''

    if isinstance(data[key], six.string_types):
        links = [link.strip()
                 for link in data[key].split(',')
                 if link.strip()]
    else:
        links = data[key]

    for link in links:
        single_link_validator(link, context)

@register_validator
def decimal_validator(value):
    ''' Checks that the provided value (if it is present) is a valid decimal '''

    if value:
        try:
            Decimal(value)
        except DecimalException:
            raise Invalid('Invalid decimal: %s' % value)
    return value

@register_validator
def spatial_resolution_validator(value):
    ''' Checks that the provided value (if it is present) is a valid spatial resolution (decimal or as representative fraction like 1:1.000) '''
    if value:
        res = [ds.strip() for ds in value.split(':') if ds.strip()]
    else:
        res = value
    
    for re in res:
        decimal_validator(re)
    return value

@scheming_validator
@register_validator
def gemet_hierarchial_tree(field, schema):

    def _scheming_multiple_text(key, data, errors, context):
        # just in case there was an error before our validator,
        # bail out here because our errors won't be useful
        if errors[key]:
            return
        value = data[key]

        language = "de"
        # get the related object
        # take a look at the languages at the end!!!
        def getRelatedObj(o_uri, type):
            if type == "broader" and o_uri:
                relation = "http://www.w3.org/2004/02/skos/core%23broader&language=" + language
                url = "https://www.eionet.europa.eu/gemet/getRelatedConcepts?concept_uri=" + o_uri + "&relation_uri=" + relation
                return requests.get(url).json()
            elif type == "group" and o_uri:
                relation = "http://www.eionet.europa.eu/gemet/2004/06/gemet-schema.rdf%23group&language=" + language
                url = "https://www.eionet.europa.eu/gemet/getRelatedConcepts?concept_uri=" + o_uri + "&relation_uri=" + relation
                return requests.get(url).json()

        def getValue(req, type):
            if req and type == "string":
                return req[0].get('preferredLabel').get('string')
            elif req and type == "uri":
                return req[0].get('uri')
            elif not req:
                return None

        def createTree(object):
            list = [getValue(object,"string")]
            uri = getValue(object,"uri")
            while getRelatedObj(uri,"broader"):
                object = getRelatedObj(uri,"broader")
                list.append(getValue(object,"string"))
                uri = getValue(object,"uri")
            
            list.append(getValue(getRelatedObj(uri,"group"),"string"))
            list.reverse()
            return list
        
        # just to unsterstand the error
        # logging.warning(type(value))
        # logging.warning(value)
        if (type(value) is unicode) and not value.startswith("{"):
            url = "https://www.eionet.europa.eu/gemet/getConceptsMatchingKeyword?keyword=" + value + "&search_mode=0&thesaurus_uri=http://www.eionet.europa.eu/gemet/concept/&language=" + language
            req = requests.get(url).json()
            # logging.warning(json.dumps(createTree(req)))
            data[key] = json.dumps(createTree(req))
        elif (type(value) is list):
            data[key] = json.dumps(value)
            
        # elif (type(value) is unicode) and  value.startswith("{"):
        #     # in case of the creation of dataset
        #     None
    return _scheming_multiple_text