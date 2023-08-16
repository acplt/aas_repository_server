import datetime
import os
import configparser
import json
import logging
from typing import Optional, List, Dict


import flask
import jwt
import werkzeug.security


from basyx.aas import model
from basyx.aas.adapter.json import json_serialization, json_deserialization
from aas_repository_server\
    import auth, storage
from aas_repository_server.access_control import check_authorization, create_OPA_input, AuthorizationError, extract_accessRights_from_submodelSecurity
from flask import  stream_with_context, abort, Response, request


# todo: Config anpassen, parsing anpassen , storage anpassen


APP = flask.Flask(__name__)
config = configparser.ConfigParser()
config.read([
    os.path.join(os.path.dirname(__file__), "config.ini"),
    os.path.join(os.path.dirname(__file__), "config.ini.default")
])


# Read config file
# JWT Expiration Time in minutes
JWT_EXPIRATION_TIME: int = int(config["AUTHENTICATION"]["TOKEN_EXPIRATION_TIME"])
PORT: int = int(config["GENERAL"]["PORT"])
AAS_STORAGE_DIR: str = os.path.abspath(config["STORAGE"]["AAS_STORAGE_DIR"])
FILE_STORAGE_DIR: str = os.path.abspath(config["STORAGE"]["FILE_STORAGE_DIR"])
# Create Storage dir, if not existing
if not os.path.exists(AAS_STORAGE_DIR):
    os.makedirs(AAS_STORAGE_DIR)
if not os.path.exists(FILE_STORAGE_DIR):
    os.makedirs(FILE_STORAGE_DIR)
OBJECT_STORE: storage.RepositoryObjectStore = storage.RepositoryObjectStore(AAS_STORAGE_DIR)

""""OPA config"""
OPA_URL=config.get("OPA", "OPA_URL", fallback="http://localhost:8181/v1/data/policy/allow")  #Load OPA URL
APP.logger.setLevel(logging.DEBUG) # Set the logging level to DEBUG


@APP.route("/login", methods=["GET", "POST"])
def login_user():
    """
    Login a user with basic authentication and respond with a new JWT, if the authentication was successful.
    """
    if not flask.request.authorization \
            or not flask.request.authorization.username \
            or not flask.request.authorization.password:
        return flask.make_response("Unauthorized", 401)
    username: str = flask.request.authorization.username
    if not auth.check_if_user_exists(username):
        print("Unknown user '{}'".format(username))
        return flask.make_response("Invalid User or Password", 401)
    if werkzeug.security.check_password_hash(auth.get_password_hash(username), flask.request.authorization.password):
        token = jwt.encode(
            {
                'name': username,
                'exp': datetime.datetime.utcnow() + datetime.timedelta(minutes=JWT_EXPIRATION_TIME)
            },
            auth.SECRET_KEY,
            algorithm="HS256"
        )
        print("User '{}' successful login".format(username))
        return flask.json.dumps({"token": token})
    else:
        print("User '{}' invalid password".format(username))
        return flask.make_response("Invalid User or Password", 401)


@APP.route("/test_connection", methods=["GET"])
def test_connection():
    """
    Returns "success", if everything is fine
    """
    return flask.make_response("Success", 200)


@APP.route("/test_authorized", methods=["GET"])
@auth.token_required
def test_authorized(current_user: str):
    """
    Tests if the connection can be established and the user is authorized

    :return:
    """
    return flask.json.dumps({"Connection": "ok", "User": current_user}), 200


@APP.route("/add_identifiable", methods=["POST"])
@auth.token_required
def add_identifiable(current_user: str):
    """
    Request format is a json serialized :class:`basyx.aas.model.base.Identifiable`:

    Add an identifiable to the repository.

    :returns:

        - 200
        - 400, if the request cannot be parsed
        - 409, if the Identifiable already exists in the OBJECT_STORE
    """
    data = flask.request.get_data(as_text=True)
    try:
        identifiable: Optional[model.Identifiable] = json.loads(data, cls=json_deserialization.AASFromJsonDecoder)
    except json.decoder.JSONDecodeError:
        return flask.make_response("Could not parse request, not valid JSON", 400)
    # Todo: Check here if the given user has access rights to the Identifiable
    try:
        input = create_OPA_input(request, current_user)
        check_authorization(APP, input,OPA_URL)
    except Exception as e:
        APP.logger.exception("Unexpected error querying OPA.")
        abort(500)
    try:
        if isinstance(identifiable, model.AssetAdministrationShell):  # insert the security metamodel automatically for new added AAS
            generate_security_submodel_template(identifiable)
        OBJECT_STORE.add(identifiable)
    except KeyError:
        return flask.make_response("Identifiable already exists in OBJECT_STORE", 200)
    return flask.make_response("Success", 200)



@APP.route("/modify_identifiable", methods=["PUT"])
@auth.token_required
def modify_identifiable(current_user: str):
    """
    Request format is a json serialized :class:`basyx.aas.model.base.Identifiable`:

    Modify an existing Identifiable by overwriting it with the given one.

    :returns:

        - 200
        - 400, if the request cannot be parsed
        - 404, if no result is found
    """
    data = flask.request.get_data(as_text=True)
    try:
        identifiable_new: Optional[model.Identifiable] = json.loads(data, cls=json_deserialization.AASFromJsonDecoder)
    except json.decoder.JSONDecodeError:
        return flask.make_response("Could not parse request, not valid JSON", 400)
    identifier: Optional[model.Identifier] = identifiable_new.identification
    identifiable_stored: Optional[model.Identifiable] = OBJECT_STORE.get(identifier)
    # Todo: Check here if the given user has access rights to the Identifiable
    try:
        input  = create_OPA_input(request, current_user, identifier.id)
        check_authorization(APP, input, OPA_URL)
    except Exception as e:
        APP.logger.exception("Unexpected error querying OPA.")
        abort(500)
    if identifiable_stored is None:
        return flask.make_response("Could not find Identifiable with id {} in repository".format(identifier.id), 404)
    identifiable_stored.update_from(identifiable_new)
    return flask.make_response("Success", 200)


@APP.route("/get_identifiable", methods=["GET"])
@auth.token_required
def get_identifiable(current_user: str):
    """
    Request format is a json serialized :class:`basyx.aas.model.base.Identifier`:

    .. code-block::

        {
            "id": "<Identifier.id string>",
            "idType": "<idType string>"
        }

    Returns a JSON serialized :class:`basyx.aas.model.base.Identifiable`.

    :returns:

        - 200, with the Identifiable
        - 400, if the request cannot be parsed
        - 404, if no result is found
        - 422, if a valid AAS object was given, but not an Identifiable
    """
    data = flask.request.get_data(as_text=True)
    # Load the JSON from the request
    try:
        identifier_dict: Dict[str, str] = json.loads(data)
    except json.decoder.JSONDecodeError:
        return flask.make_response("Could not parse request, not valid JSON", 400)
    # Check that the request JSON contained in fact an Identifier
    try:
        identifier: model.Identifier = model.Identifier(
            id_=identifier_dict["id"],
            id_type=json_deserialization.IDENTIFIER_TYPES_INVERSE[identifier_dict["idType"]]
        )
    except KeyError:
        return flask.make_response("Request does not contain an Identifier", 422)
    # Try to resolve the Identifier in the object store
    identifiable: Optional[model.Identifiable] = OBJECT_STORE.get(identifier)
    # Todo: Check here if the given user has access rights to the Identifiable
    ressource_identifier, submodels_security = extract_accessRights_from_submodelSecurity(identifiable)
    print("Ressource Identifier:", ressource_identifier)
    print("Submodel Security Roles:", submodels_security)
    try:
        input  = create_OPA_input(request, current_user, identifier.id) # type(identifiable)
        check_authorization(APP, input,OPA_URL)
    except Exception as e:
        APP.logger.exception("Unexpected error querying OPA.")
        abort(500)
    if identifiable is None:
        return flask.make_response("Could not find Identifiable with id {} in repository".format(identifier.id), 404)
    return flask.make_response(
        json.dumps(identifiable, cls=json_serialization.AASToJsonEncoder, indent=4),
            200)


@APP.route("/get_file", methods=["GET"])
@auth.token_required
def get_file(current_user: str):
    """
    Request format is a String IRI

    Returns a File from the FILE_STORAGE_DIR.

    :returns:

        - 200, with the File
        - 404, if no result is found
    """
    file_iri = flask.request.get_data(as_text=True)
    file_iri = file_iri.strip('"')
    file_path_iri = file_iri.removeprefix('file:/')
    file_path: str = FILE_STORAGE_DIR+"/"+file_path_iri
    if not os.path.isfile(file_path):
        return flask.make_response("Could not fetch File with IRI {}".format(file_iri), 404)

    def generate():
        with open(file_path, mode='rb', buffering=4096) as myFmu:
            for chunk in myFmu:
                yield chunk
    return Response(stream_with_context(generate()))


@APP.route("/post_file", methods=["POST"])
@auth.token_required
def add_file(current_user: str):
    """
    Request format is a streamed File:

    Add an File to the FILE_STORAGE_DIR.

    :returns:

        - 200, and the IRI of the added FMU-File
        - 404, if the Path of the IRI does not exist
    """
    data = flask.request.get_data(cache=False)
    file_name = flask.request.headers.get("name")
    path_with_file = FILE_STORAGE_DIR+"/"+file_name
    print(path_with_file)
    with open(path_with_file, 'wb', buffering=4096) as myFmu:
        myFmu.write(data)
    file_iri: str = "file:"+file_name
    return flask.make_response(file_iri, 200)


@APP.route("/query_semantic_id", methods=["GET"])
@auth.token_required
def query_semantic_id(current_user: str):
    """
    Query the repository for a contained semanticID.

    Specify which attributes of the semanticID should be checked.
    Request format is a json serialized :class:`basyx.aas.model.base.Key` (from a Reference):

    .. code-block::

        {
            'semantic_id': {
                'type': 'GlobalReference',
                'idType': 'IRI',
                'value': 'https://example.com/semanticIDs/ONE',
                'local': False
            }
            'check_for_key_type': false,
            'check_for_key_local': false,
            'check_for_key_id_type': false,
        }

    Returns a list of Identifiers of the identifiable the semanticID is contained in
    and optionally, the Identifier of the parent AssetAdministrationShell, if it exists.

    .. code-block::

        [
            {
                'identifier': {
                    "id": "<Identifier.id string>",
                    "idType": "<idType string>"
                },
                'asset_administration_shell': {
                    "id": "<Identifier.id string>",
                    "idType": "<idType string>"
                }
            }
        ]

    :returns:

        - 200, with the above result
        - 400, if the request cannot be parsed
        - 422, if a valid AAS object was given, but not an Identifiable
    """
    data = flask.request.get_data(as_text=True)
    # Load the JSON from the request
    try:
        data_dict: Dict = json.loads(data)
    except json.decoder.JSONDecodeError:
        return flask.make_response("Could not parse request, not valid JSON", 400)
    # Check that the request has all the required fields
    try:
        check_for_key_type: bool = data_dict["check_for_key_type"]
        check_for_key_local: bool = data_dict["check_for_key_local"]
        check_for_key_id_type: bool = data_dict["check_for_key_id_type"]
        semantic_id: model.Key = model.Key(
            type_=json_deserialization.KEY_ELEMENTS_INVERSE[data_dict["semantic_id"]["type"]],
            local=True if data_dict["semantic_id"] else False,
            value=data_dict["semantic_id"]["value"],
            id_type=json_deserialization.KEY_TYPES_INVERSE[data_dict["semantic_id"]["idType"]]
        )
    except KeyError:
        return flask.make_response("Request does not have correct format", 422)
    # Get the list of identifiables that contain the semanticID
    result = OBJECT_STORE.get_semantic_id(
        semantic_id=semantic_id,
        check_for_key_type=check_for_key_type,
        check_for_key_local=check_for_key_local,
        check_for_key_id_type=check_for_key_id_type
    )
    # Todo: Check here if the given user has access rights to the Identifiable
    jsonable_result: List = []
    for semantic_index_element in result:
        print(semantic_index_element.semantically_identified_referable)
        try:
            input = create_OPA_input(request, current_user,  semantic_index_element.parent_identifiable.id)
            check_authorization(APP, input, OPA_URL)
        except Exception as e:
            APP.logger.exception("Unexpected error querying OPA.")
            abort(500)
        jsonable_result.append(
            {
                "identifier": semantic_index_element.parent_identifiable,
                "asset_administration_shell": semantic_index_element.parent_asset_administration_shell
            }
        )
    return flask.make_response(
        json.dumps(
            jsonable_result,
            cls=json_serialization.AASToJsonEncoder,
            indent=4
        ),
        200
    )
def generate_security_submodel_template(aas_id: model.AssetAdministrationShell):
    """""
    How to refer to the others submodels in AAS?
    # Iterate through the submodels of the given AAS and add references to them
    # the same can be done for the attribute
    submodel_references = []  # Initialize an empty list to store the references
    for submodel in aas_id.submodel:
        submodel_reference = model.SubmodelReference.from_referable(submodel)
        submodel_references.append(submodel_reference)
        # Create Element 3 to refer to the Security Submodel itself
        element_3 = model.SubmodelElement(
            id_short="submodelSecurity_accessRights"
        )
        element_3_value = model.AASReference.from_referable(submodel_Security.identification)
        element_3.value = element_3_value
        submodel_Security.submodel_element.append(element_3)
"""""
    submodel_Security = model.Submodel(
        identification=model.Identifier('https://acplt.org/Security_Submodel', model.IdentifierType.IRI),
        id_short="SecuritySubmodel",
        submodel_element={
            model.Property(
                id_short='ressource',
                value_type=model.datatypes.String,
                value=aas_id.identification.id
            ),
            model.Property(
                id_short='read',
                value_type=model.datatypes.String,
                value=str(['admin', 'rwthStudent', 'otherStudent']),
            ),
            model.Property(
                id_short='modify',
                value_type=model.datatypes.String,
                value=str(['admin']),
            ),
        }
    )
    #add the security submodel to the given aas
    OBJECT_STORE.add(submodel_Security)
    aas_id.submodel.add(model.AASReference.from_referable(submodel_Security))

#configure the logging
logger = logging.getLogger('audit')
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    filename='audit.log',  # Specify the file to store audit logs
    filemode='a'  # Append mode
)

# Create a FileHandler for the audit logger
file_handler = logging.FileHandler('audit.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)


@APP.after_request
def after_request(response):
    """ execute this after each request to extract relevant information from the request and response to create an audit log. """

    logger.info({
                          "time": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                          "user_ip": request.remote_addr,
                          #"user_name": g.user,
                          "method": request.method,
                          "request_url": request.path,
                          "response_status": response.status}
    )
    return response



@APP.errorhandler(AuthorizationError)
def handle_authorization_error(error):
    print("test this")
    return flask.make_response("Authorization Failed", 401)

if __name__ == '__main__':
    print("Running with configuration: {}".format({s: dict(config.items(s)) for s in config.sections()}))
    print("Found {} Users".format(len(auth.USERS)))
    APP.run(port=PORT)


